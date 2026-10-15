import json
import threading
import urllib.request

import pytest

from steward import mcp_server
from steward.api import StewardAPI
from steward.mcp_server import MCPServer, build_tools
from steward.store import Store


def _call(port, method, params=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


@pytest.fixture
def tunnel_server(steward_home):
    store = Store(steward_home)
    server = MCPServer(build_tools(StewardAPI(store)), home=steward_home,
                       anonymous_scopes=("runner:read", "runner:steer", "runner:pause"))
    started = threading.Event()
    holder = {}

    def ready(httpd):
        holder["httpd"] = httpd
        started.set()

    t = threading.Thread(target=lambda: mcp_server.serve(server, port=0, ready=ready), daemon=True)
    t.start()
    started.wait(5)
    yield holder["httpd"].server_address[1], store
    holder["httpd"].shutdown()


def test_tunnel_mode_needs_no_credential_and_lists_tools(tunnel_server):
    port, _ = tunnel_server
    assert len(_call(port, "tools/list")["result"]["tools"]) == 33


def test_tunnel_mode_scopes_are_fixed_cancel_refused(tunnel_server):
    port, store = tunnel_server
    from steward.control import Controller

    job = Controller(store).new_job("g", "/tmp")
    r = _call(port, "tools/call", {"name": "cancel_job", "arguments": {"job_id": job.job_id}})
    assert r["error"]["message"] == "insufficient_scope"


def test_tunnel_mode_refuses_public_serving(steward_home):
    server = MCPServer([], home=steward_home, anonymous_scopes=("runner:read",))
    with pytest.raises(ValueError):
        mcp_server.serve(server, port=0, public_base_url="https://x.example", passphrase_record={"a": 1})


def test_cli_rejects_admin_scope_in_tunnel_mode(steward_home, capsys):
    from steward import cli

    assert cli.main(["mcp", "serve-tunnel", "--scopes", "runner:read,runner:admin"]) == 2
