"""Phase 19-06 (G13, G14): the other seven components: prerequisites, plans, recorded effects, typed confirmations.

FakeRunner for systemctl, tg, herdr and cloudflared; FakeHerdr for the Herdr calls; temp HOME, XDG_CONFIG_HOME,
STEWARD_HOME and config dir; hidden input and prompts come from scripted callables. No network, no real binaries.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

from steward import components, installstate, mcp_auth, templates_io
from steward.runner_exec import CommandResult
from test_components import Env

HOST = "steward.example.test"
TUNNEL_ID = "0a1b2c3d-1111-2222-3333-444455556666"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    (e.config / "steward.toml").write_text(
        "# config\nschema = 1\n\n[jev]\n# advisory only\nmode = \"shadow\"\n\n[alerts]\n# telegram\ntelegram = false\n")
    return e


def _exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def _enable_mcp(env):
    assert components.enable("mcp", env.ctx(yes=True)) == 0, env.text()
    env.out.clear()
    env.misc.clear()


def _which(missing=()):
    return lambda n: None if n in missing else f"/usr/bin/{n}"


# -- telegram -------------------------------------------------------------------------------------------------------


def test_telegram_is_blocked_without_tg_or_with_a_failing_status(env):
    assert components.enable("telegram", env.ctx(yes=True, which=_which(("tg",)))) == 1
    assert "tg" in env.text() and "not installed" in env.text()
    env.out.clear()
    env.fail["tg status"] = CommandResult([], ".", 1, "", "no chat", 0.0)
    assert components.enable("telegram", env.ctx(yes=True)) == 1
    assert "`tg status` failed" in env.text()
    assert "telegram = false" in (env.config / "steward.toml").read_text()


def test_telegram_sets_the_alert_flag_and_changes_nothing_else(env):
    path = env.config / "steward.toml"
    before = path.read_text().splitlines()
    assert components.enable("telegram", env.ctx(yes=True)) == 0, env.text()
    after = path.read_text().splitlines()
    assert len(before) == len(after)
    assert [(a, b) for a, b in zip(before, after) if a != b] == [("telegram = false", "telegram = true")]
    assert ["tg", "status"] in env.misc
    assert env.state["components"]["telegram"]["enabled"] is True
    assert components.disable("telegram", env.ctx()) == 0
    assert "telegram = false" in path.read_text() and "telegram = true" not in path.read_text()
    assert env.state["components"]["telegram"]["enabled"] is False


# -- jev ------------------------------------------------------------------------------------------------------------


def test_jev_writes_the_key_into_keys_env_and_never_shows_it(env):
    secret = "tsk-very-secret-value-123"
    env.secrets["TypeSafe"] = secret
    assert components.enable("jev", env.ctx(yes=True)) == 0, env.text()
    keys_env = env.config / "keys.env"
    assert f"TYPESAFE_API_KEY={secret}" in keys_env.read_text()
    assert stat.S_IMODE(keys_env.stat().st_mode) == 0o600
    assert secret not in env.text()
    assert secret not in (env.state_dir / "install.json").read_text()
    assert secret not in repr(components.plan("jev", env.ctx())) and secret not in components.plan("jev", env.ctx()).text()
    assert 'mode = "shadow"' in (env.config / "steward.toml").read_text()
    assert components.disable("jev", env.ctx()) == 0
    assert 'mode = "off"' in (env.config / "steward.toml").read_text()
    assert f"TYPESAFE_API_KEY={secret}" in keys_env.read_text()  # the key stays


def test_jev_does_not_duplicate_an_existing_key_line(env):
    (env.config / "keys.env").write_text("TYPESAFE_API_KEY=already\n")
    (env.config / "keys.env").chmod(0o600)
    assert components.enable("jev", env.ctx(yes=True)) == 0  # no secret prompt: a missing one would raise
    assert (env.config / "keys.env").read_text().count("TYPESAFE_API_KEY") == 1


def test_jev_reenable_after_disable_restores_shadow(env):
    env.secrets["TypeSafe"] = "k1"
    components.enable("jev", env.ctx(yes=True))
    components.disable("jev", env.ctx())
    assert components.enable("jev", env.ctx(yes=True)) == 0
    assert 'mode = "shadow"' in (env.config / "steward.toml").read_text()


def test_jev_with_no_key_entered_changes_nothing(env):
    env.secrets["TypeSafe"] = "  "
    assert components.enable("jev", env.ctx(yes=True)) == 1
    assert not (env.config / "keys.env").exists()


# -- moshi ----------------------------------------------------------------------------------------------------------


def test_moshi_opens_the_phone_tab_and_prints_the_link(env):
    assert components.enable("moshi", env.ctx(yes=True)) == 0, env.text()
    (tab,) = env.fake.tabs_labelled("📱 phone")
    assert "moshi://herdr?workspace=w1" in env.text()
    assert "📱 phone" in env.state["tabs"]
    assert env.state["components"]["moshi"]["tab"] == "📱 phone"
    assert "moshi://" not in (env.state_dir / "install.json").read_text()
    assert components.disable("moshi", env.ctx()) == 0
    assert env.fake.tabs_labelled("📱 phone") == []
    assert "📱 phone" not in env.state["tabs"]


def test_moshi_is_blocked_outside_herdr(env):
    assert components.enable("moshi", env.ctx(yes=True, workspace=None)) == 1
    assert "Herdr" in env.text() and env.fake.tabs == []


# -- android --------------------------------------------------------------------------------------------------------


def test_android_needs_mcp(env):
    assert components.enable("android", env.ctx(yes=True)) == 1
    assert "mcp must be enabled first" in env.text()


def test_android_prints_the_token_once_and_records_only_its_id(env):
    _enable_mcp(env)
    assert components.enable("android", env.ctx(yes=True)) == 0, env.text()
    (entry,) = mcp_auth.list_tokens(home=env.state_dir)
    assert entry["scopes"] == ["runner:read", "runner:steer", "runner:pause"]
    token = re.search(r"token: (\S+)", env.text()).group(1)
    assert env.text().count(token) == 1
    assert "http://127.0.0.1:8765/mcp" in env.text()
    blob = (env.state_dir / "install.json").read_text()
    assert token not in blob and entry["id"] in blob
    assert env.state["components"]["android"]["token_id"] == entry["id"]
    env.out.clear()
    assert components.enable("android", env.ctx(yes=True)) == 1
    assert "token already created: revoke with steward disable android" in env.text()
    assert components.disable("android", env.ctx()) == 0
    assert mcp_auth.list_tokens(home=env.state_dir)[0]["revoked"] is True


def test_android_uses_the_public_url_when_the_tunnel_is_enabled(env):
    _enable_mcp(env)
    state = env.state
    installstate.record_component(state, "tunnel", enabled=True, hostname=HOST, mode="systemd", units=[], files=[])
    env.save_state(state)
    assert components.enable("android", env.ctx(yes=True)) == 0
    assert f"https://{HOST}/mcp" in env.text()


# -- herdr-plugin ---------------------------------------------------------------------------------------------------


def test_herdr_plugin_links_the_bundled_manifest(env, tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    assert components.enable("herdr-plugin", env.ctx(yes=True, install_root=root)) == 1
    assert "no herdr-plugin.toml in this install" in env.text()
    (root / "herdr-plugin.toml").write_text('id = "steward-plugin"\n')
    env.out.clear()
    assert components.enable("herdr-plugin", env.ctx(yes=True, install_root=root)) == 0, env.text()
    assert ["herdr", "plugin", "link", str(root)] in env.misc
    assert env.state["components"]["herdr-plugin"]["plugin_id"] == "steward-plugin"
    assert env.state["plugin_links"] == [{"id": "steward-plugin", "path": str(root)}]
    assert components.disable("herdr-plugin", env.ctx()) == 0
    assert ["herdr", "plugin", "unlink", "steward-plugin"] in env.misc
    assert env.state["plugin_links"] == []


# -- tunnel ---------------------------------------------------------------------------------------------------------


def _tunnel_answers(env, tmp_path):
    cloudflared = _exe(tmp_path / "bin" / "cloudflared")
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    env.answers.update({"Type the": HOST, "hostname": HOST, "binary": cloudflared, "tunnel id": TUNNEL_ID,
                        "credentials": str(creds)})
    return cloudflared, creds


def test_tunnel_needs_mcp_first(env, tmp_path):
    _tunnel_answers(env, tmp_path)
    assert components.enable("tunnel", env.ctx(interactive=True)) == 1
    assert "mcp must be enabled first" in env.text()


def test_tunnel_yes_alone_is_refused_and_a_wrong_string_too(env, tmp_path):
    _enable_mcp(env)
    _tunnel_answers(env, tmp_path)
    assert components.enable("tunnel", env.ctx(yes=True)) == 1
    assert "must be typed every time" in env.text() and "--yes does not satisfy" in env.text()
    env.out.clear()
    assert components.enable("tunnel", env.ctx(yes=True, confirm_text="something-else")) == 1
    env.answers["Type the"] = "nope.example.test"
    env.out.clear()
    assert components.enable("tunnel", env.ctx(yes=True, interactive=True)) == 1
    assert not (env.config / "tunnel").exists() and not (env.config / "mcp.json").exists()
    assert not (env.unit_dir / "steward-tunnel.service").exists()
    assert "tunnel" not in env.state["components"]


def test_tunnel_with_the_typed_hostname(env, tmp_path):
    _enable_mcp(env)
    cloudflared, creds = _tunnel_answers(env, tmp_path)
    assert components.enable("tunnel", env.ctx(confirm_text=HOST)) == 0, env.text()
    cfg = env.config / "tunnel" / "cloudflared.yml"
    text = cfg.read_text()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert f"tunnel: {TUNNEL_ID}" in text and f"credentials-file: {creds}" in text
    assert text.count("hostname:") == 1 and f"hostname: {HOST}" in text
    assert "service: http://127.0.0.1:9520" in text and "service: http_status:404" in text
    mcp_cfg = json.loads((env.config / "mcp.json").read_text())
    assert mcp_cfg["public_base_url"] == f"https://{HOST}" and mcp_cfg["port"] == 9520
    assert (tmp_path / "data" / "consent.secret").exists()
    secret = (tmp_path / "data" / "consent.secret").read_text().strip()
    assert secret not in env.text() and "consent.secret" in env.text()
    unit = (env.unit_dir / "steward-tunnel.service").read_text()
    assert "service run tunnel" in unit and "EnvironmentFile" not in unit
    assert env.systemctl() == ["daemon-reload", "enable --now steward-tunnel", "restart steward-mcp"]
    assert f"cloudflared tunnel route dns {TUNNEL_ID} {HOST}" in env.text()
    assert not any("route" in " ".join(a) or "create" in " ".join(a) for a in env.misc)
    rec = env.state["components"]["tunnel"]
    assert rec["enabled"] and rec["units"] == ["steward-tunnel"] and rec["hostname"] == HOST
    assert str(cfg) in rec["files"]
    # the argv the unit runs is exact, and `mcp` now starts in config mode
    state = env.state
    assert components.service_argv("tunnel", state, env.config) == [
        cloudflared, "tunnel", "--no-autoupdate", "--config", str(cfg), "run"]
    assert components.service_argv("mcp", state, env.config)[-2:] == ["--config", str(env.config / "mcp.json")]
    # disable removes the unit and the generated config, keeps the connector config and the passphrase
    assert components.disable("tunnel", env.ctx()) == 0
    assert not cfg.exists() and not (env.unit_dir / "steward-tunnel.service").exists()
    assert (env.config / "mcp.json").exists() and (tmp_path / "data" / "consent.secret").exists()


def test_tunnel_typed_interactively(env, tmp_path):
    _enable_mcp(env)
    _tunnel_answers(env, tmp_path)
    assert components.enable("tunnel", env.ctx(interactive=True)) == 0, env.text()


def test_tunnel_rejects_a_bad_hostname_and_a_missing_binary(env, tmp_path):
    _enable_mcp(env)
    _tunnel_answers(env, tmp_path)
    env.answers["hostname"] = "https://x.example/"
    env.answers["Type the"] = "https://x.example/"
    assert components.enable("tunnel", env.ctx(interactive=True)) == 1
    assert "hostname" in env.text()
    env.answers.update({"hostname": HOST, "Type the": HOST, "binary": str(tmp_path / "nope")})
    env.out.clear()
    assert components.enable("tunnel", env.ctx(interactive=True)) == 1
    assert "cloudflared" in env.text() and "not found" in env.text()


def test_tunnel_in_herdr_mode_restarts_the_mcp_pane(env, tmp_path):
    assert components.enable("mcp", env.ctx(systemd=False, yes=True)) == 0
    _tunnel_answers(env, tmp_path)
    before = len(env.fake.runs()[env.fake.pane_by_label("svc mcp")["pane_id"]])
    assert components.enable("tunnel", env.ctx(systemd=False, confirm_text=HOST)) == 0, env.text()
    assert env.fake.pane_by_label("svc tunnel") is not None
    assert len(env.fake.runs()[env.fake.pane_by_label("svc mcp")["pane_id"]]) == before + 1  # restarted


# -- openai-tunnel --------------------------------------------------------------------------------------------------


def test_openai_tunnel(env, tmp_path):
    _enable_mcp(env)
    client = _exe(tmp_path / "opt" / "tunnel-client")
    key = "oai-secret-key-999"
    env.answers.update({"Type the": "openai-tunnel", "client": client, "Profile name": "prof1"})
    env.secrets["OPENAI_TUNNEL_API_KEY"] = key
    assert components.enable("openai-tunnel", env.ctx(yes=True)) == 1  # --yes never satisfies it
    assert components.enable("openai-tunnel", env.ctx(confirm_text="openai-tunnel")) == 0, env.text()
    assert f"OPENAI_TUNNEL_API_KEY={key}" in (env.config / "keys.env").read_text()
    assert key not in env.text() and key not in (env.state_dir / "install.json").read_text()
    local = (env.unit_dir / "steward-mcp-tunnel-local.service").read_text()
    remote = (env.unit_dir / "steward-openai-tunnel.service").read_text()
    assert "service run mcp-tunnel" in local and "service run openai-tunnel" in remote
    assert "Requires=steward-mcp-tunnel-local.service" in remote
    assert key not in local + remote and "EnvironmentFile" not in local + remote
    assert env.systemctl() == ["daemon-reload", "enable --now steward-mcp-tunnel-local",
                               "enable --now steward-openai-tunnel"]
    state = env.state
    assert components.service_argv("mcp-tunnel", state, env.config)[-4:] == ["mcp", "serve-tunnel", "--port", "9521"]
    assert components.service_argv("openai-tunnel", state, env.config) == [client, "run", "--profile", "prof1"]
    assert components.service_env("openai-tunnel", lambda n: key if n == "OPENAI_TUNNEL_API_KEY" else None) == {
        "CONTROL_PLANE_API_KEY": key}
    env.misc.clear()
    assert components.disable("openai-tunnel", env.ctx()) == 0
    assert env.systemctl()[:2] == ["disable --now steward-openai-tunnel", "disable --now steward-mcp-tunnel-local"]
    assert not (env.unit_dir / "steward-openai-tunnel.service").exists()


def test_openai_tunnel_takes_an_existing_key_without_asking(env, tmp_path):
    _enable_mcp(env)
    (env.config / "keys.env").write_text("OPENAI_TUNNEL_API_KEY=present\n")
    (env.config / "keys.env").chmod(0o600)
    client = _exe(tmp_path / "opt" / "tunnel-client")
    env.answers.update({"client": client, "Profile name": ""})
    assert components.enable("openai-tunnel", env.ctx(confirm_text="openai-tunnel")) == 0, env.text()
    assert env.state["components"]["openai-tunnel"]["profile"] == "steward"


def test_service_env_without_the_key_is_an_error():
    with pytest.raises(components.ComponentError):
        components.service_env("openai-tunnel", lambda n: None)
    assert components.service_env("mcp", lambda n: None) == {}


# -- templates and the table ----------------------------------------------------------------------------------------


def test_unit_templates_name_nobody():
    directory = components.TEMPLATE_DIR
    names = sorted(p.name for p in directory.glob("*.service"))
    assert names == ["steward-mcp-tunnel-local.service", "steward-mcp.service", "steward-openai-tunnel.service",
                     "steward-tunnel.service"]
    for path in directory.glob("*.service"):
        text = path.read_text()
        for bad in ("/home/", "cairn" "labs", ".intern.", "EnvironmentFile", "agent", TUNNEL_ID[:8], "@", "/bin/"):
            assert bad not in text, (path.name, bad)
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text.replace("127.0.0.1", ""))
        assert "NoNewPrivileges=yes" in text
        assert "{exec_start}" in text


def test_every_component_has_an_off_reason_and_a_disable(env):
    for cid, on, text in components.status(env.state):
        assert not on and text.startswith("off (") and text.endswith(")"), cid
    before = (env.state_dir / "install.json").read_text()
    for cid in components.COMPONENT_IDS:
        env.out.clear()
        assert components.disable(cid, env.ctx()) == 0
        assert "not enabled" in env.text()
    assert (env.state_dir / "install.json").read_text() == before
    assert env.misc == []


def test_every_component_has_a_planner_and_an_applier():
    for cid in components.COMPONENT_IDS:
        assert components.PLANNERS[cid] is not components._not_yet, cid
        assert cid in components.APPLIERS, cid
