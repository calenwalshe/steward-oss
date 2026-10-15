"""Ops surface for the public connector (LIVE-04): bootstrap, doctor, serve
--config, systemd units, tunnel template. All paths are tmp dirs (HOME, XDG
and STEWARD_* are redirected), nothing is installed, enabled or started, and
no network beyond loopback is used."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from steward import cli, mcp_server, oauth

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
URL = "https://steward-mcp.example.test"


@pytest.fixture
def ops(tmp_path, monkeypatch, steward_home):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("STEWARD_DATA_DIR", str(data_dir))
    return {"config": config_dir, "data": data_dir, "home": steward_home}


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def bootstrap(port=None, url=URL):
    argv = ["mcp", "bootstrap", "--public-base-url", url]
    if port is not None:
        argv += ["--port", str(port)]
    return cli.main(argv)


# -- bootstrap ----------------------------------------------------------------------


def test_bootstrap_writes_config_secret_and_hash(ops, capsys):
    assert bootstrap() == 0
    cfg_path = ops["config"] / "mcp.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["public_base_url"] == URL
    assert cfg["port"] == 9520
    assert cfg["trust_forwarded"] is True
    secret_path = ops["data"] / "consent.secret"
    hash_path = ops["config"] / "consent.hash"
    assert _mode(secret_path) == 0o600
    assert _mode(hash_path) == 0o600
    secret = secret_path.read_text().strip()
    assert len(secret) >= 24
    record = json.loads(hash_path.read_text())
    assert oauth.verify_passphrase(record, secret)
    assert secret not in hash_path.read_text()
    assert secret not in cfg_path.read_text()
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    assert str(secret_path) in out.out


def test_bootstrap_is_idempotent_and_never_rotates(ops):
    assert bootstrap() == 0
    secret_before = (ops["data"] / "consent.secret").read_bytes()
    hash_before = (ops["config"] / "consent.hash").read_bytes()
    cfg_before = (ops["config"] / "mcp.json").read_bytes()
    assert bootstrap() == 0
    assert (ops["data"] / "consent.secret").read_bytes() == secret_before
    assert (ops["config"] / "consent.hash").read_bytes() == hash_before
    assert (ops["config"] / "mcp.json").read_bytes() == cfg_before


def test_bootstrap_keeps_secret_when_only_port_changes(ops):
    assert bootstrap() == 0
    secret_before = (ops["data"] / "consent.secret").read_bytes()
    assert bootstrap(port=9621) == 0
    assert json.loads((ops["config"] / "mcp.json").read_text())["port"] == 9621
    assert (ops["data"] / "consent.secret").read_bytes() == secret_before


def test_bootstrap_rebuilds_missing_hash_from_existing_secret(ops):
    assert bootstrap() == 0
    secret = (ops["data"] / "consent.secret").read_text().strip()
    (ops["config"] / "consent.hash").unlink()
    assert bootstrap() == 0
    assert (ops["data"] / "consent.secret").read_text().strip() == secret
    assert oauth.verify_passphrase(json.loads((ops["config"] / "consent.hash").read_text()), secret)


def test_bootstrap_refuses_when_hash_mismatches_secret(ops):
    assert bootstrap() == 0
    (ops["config"] / "consent.hash").write_text(json.dumps(oauth.hash_passphrase("something else")))
    before = (ops["data"] / "consent.secret").read_bytes()
    assert bootstrap() != 0
    assert (ops["data"] / "consent.secret").read_bytes() == before


def test_bootstrap_refuses_hash_without_secret(ops):
    assert bootstrap() == 0
    (ops["data"] / "consent.secret").unlink()
    assert bootstrap() != 0
    assert not (ops["data"] / "consent.secret").exists()


def test_bootstrap_rejects_non_https_url(ops):
    assert bootstrap(url="http://steward-mcp.example.test") != 0
    assert not (ops["config"] / "mcp.json").exists()


# -- doctor ---------------------------------------------------------------------------


def _config_port(ops, port):
    cfg_path = ops["config"] / "mcp.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["port"] = port
    cfg_path.write_text(json.dumps(cfg))


def _tree(*roots):
    snap = {}
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            st = p.stat()
            snap[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return snap


def test_doctor_ok_on_good_config_and_is_non_mutating(ops, capsys):
    bootstrap(port=_free_port())
    before = _tree(ops["config"], ops["data"], ops["home"])
    assert cli.main(["mcp", "doctor"]) == 0
    assert _tree(ops["config"], ops["data"], ops["home"]) == before
    out = capsys.readouterr().out
    assert "FAIL" not in out
    secret = (ops["data"] / "consent.secret").read_text().strip()
    assert secret not in out


def test_doctor_fails_without_config(ops):
    assert cli.main(["mcp", "doctor"]) == 1


def test_doctor_fails_on_loose_secret_perms(ops, capsys):
    bootstrap(port=_free_port())
    os.chmod(ops["data"] / "consent.secret", 0o644)
    assert cli.main(["mcp", "doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_doctor_fails_on_hash_mismatch(ops):
    bootstrap(port=_free_port())
    (ops["config"] / "consent.hash").write_text(json.dumps(oauth.hash_passphrase("other")))
    os.chmod(ops["config"] / "consent.hash", 0o600)
    assert cli.main(["mcp", "doctor"]) == 1


def test_doctor_fails_on_loose_oauth_db_perms(ops):
    bootstrap(port=_free_port())
    db = ops["home"] / "oauth.db"
    db.write_bytes(b"")
    os.chmod(db, 0o644)
    assert cli.main(["mcp", "doctor"]) == 1


def test_doctor_accepts_port_held_by_steward(ops):
    server = mcp_server.MCPServer([], home=ops["home"])
    httpd = mcp_server.make_http_server(server, port=0, access_log=lambda line: None)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        bootstrap(port=httpd.server_address[1])
        assert cli.main(["mcp", "doctor"]) == 0
    finally:
        httpd.shutdown()
        t.join(timeout=5)
        httpd.server_close()


def test_doctor_fails_on_port_held_by_something_else(ops):
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Other)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        bootstrap(port=httpd.server_address[1])
        assert cli.main(["mcp", "doctor"]) == 1
    finally:
        httpd.shutdown()
        t.join(timeout=5)
        httpd.server_close()


# -- serve --config ---------------------------------------------------------------------


def test_serve_config_passes_public_settings(ops, monkeypatch):
    bootstrap(port=9533)
    seen = {}

    def fake_serve(server, **kwargs):
        seen["server"] = server
        seen.update(kwargs)

    monkeypatch.setattr(mcp_server, "serve", fake_serve)
    assert cli.main(["mcp", "serve", "--config", str(ops["config"] / "mcp.json")]) == 0
    assert seen["port"] == 9533
    assert seen["public_base_url"] == URL
    assert seen["trust_forwarded"] is True
    assert oauth.verify_passphrase(seen["passphrase_record"], (ops["data"] / "consent.secret").read_text().strip())
    assert len(seen["server"].tools) == 33


def test_serve_config_missing_file_fails(ops):
    assert cli.main(["mcp", "serve", "--config", str(ops["config"] / "nope.json")]) != 0


def test_serve_without_config_stays_local(ops, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda server, **kw: seen.update(kw))
    assert cli.main(["mcp", "serve", "--port", "8799"]) == 0
    assert seen["port"] == 8799 and seen.get("public_base_url") is None


def test_oauth_revoke_all(ops, capsys):
    from steward.oauth_store import OAuthStore, digest

    store = OAuthStore(home=ops["home"])
    store.register_client("c1", "ChatGPT", ["https://chatgpt.com/cb"], ["runner:read"])
    store.create_code(code_digest=digest("c"), tx_id="t", client_id="c1", redirect_uri="https://chatgpt.com/cb",
                      redirect_explicit=True, code_challenge="x" * 43, scopes=["runner:read"],
                      resource=URL + "/mcp", family_id="f")
    store.exchange_code(digest("c"), access_digest=digest("a"), access_id="a", refresh_digest=digest("r"),
                        refresh_id="r", access_ttl=3600, refresh_ttl=100)
    store.close()
    assert cli.main(["mcp", "oauth", "revoke-all"]) == 0
    store = OAuthStore(home=ops["home"])
    try:
        assert store.get_access(digest("a"))["revoked"] == 1
    finally:
        store.close()


# -- deploy artifacts ---------------------------------------------------------------------


def test_wrapper_is_posix_sh_and_passes_sh_n():
    wrapper = DEPLOY / "run-steward-mcp.sh"
    text = wrapper.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "set -eu" in text
    assert "exec " in text and "-m steward.cli mcp serve --config" in text
    assert "api-keys" not in text.replace("does NOT source ~/.api-keys", "")
    assert os.access(wrapper, os.X_OK)
    assert subprocess.run(["sh", "-n", str(wrapper)]).returncode == 0
    if shutil.which("dash"):
        assert subprocess.run(["dash", "-n", str(wrapper)]).returncode == 0


def _unit(name):
    lines = [l.strip() for l in (DEPLOY / name).read_text().splitlines()]
    return [l for l in lines if l and not l.startswith("#")]


def test_mcp_unit():
    lines = _unit("steward-mcp.service")
    assert any(l.startswith("ExecStart=") and l.endswith("deploy/run-steward-mcp.sh") for l in lines)
    for want in ("Restart=always", "RestartSec=3", "NoNewPrivileges=yes", "WantedBy=default.target"):
        assert want in lines
    assert not any(l.startswith("EnvironmentFile=") for l in lines)


def test_tunnel_unit():
    lines = _unit("steward-tunnel.service")
    assert ("ExecStart=/home/user/bin/cloudflared tunnel --no-autoupdate "
            "--config %h/.cloudflared/steward-mcp.yml run") in lines
    assert "Restart=always" in lines
    assert not any(l.startswith("EnvironmentFile=") for l in lines)


def test_cloudflared_template_single_ingress_and_404_catch_all():
    text = (DEPLOY / "cloudflared-steward.yml.example").read_text()
    assert "<TUNNEL_ID>" in text
    services = [l.strip() for l in text.splitlines() if l.strip().startswith("- ") or "service:" in l]
    hostnames = [l for l in text.splitlines() if "hostname:" in l and not l.strip().startswith("#")]
    assert len(hostnames) == 1 and "steward-mcp.example.org" in hostnames[0]
    assert "service: http://127.0.0.1:9520" in text
    assert text.rstrip().endswith("service: http_status:404")
    assert services
