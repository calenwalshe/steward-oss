"""Public serving hardening for the MCP server (LIVE-02, LIVE-03).

In-process `ThreadingHTTPServer` on 127.0.0.1 port 0 with
public_base_url=https://example.test, driven with `http.client` so tests can
set the Host header, send chunked bodies and forged forwarding headers. No
network beyond loopback; every server is shut down.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from steward import mcp_auth, mcp_server, oauth
from steward.oauth_store import OAuthStore

BASE = "https://example.test"
HOST = "example.test"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
PASSPHRASE = "owner passphrase for tests"
PRM = 'Bearer resource_metadata="https://example.test/.well-known/oauth-protected-resource/mcp"'


@pytest.fixture(scope="module")
def record():
    return oauth.hash_passphrase(PASSPHRASE)


def _tools():
    def read_handler(arguments, token_info):
        return {"ok": True, "token_id": token_info.id}

    def cancel_handler(arguments, token_info):
        return {"cancelled": True}

    return [
        mcp_server.ToolSpec("read_thing", "read", {"type": "object"}, {"readOnlyHint": True}, "runner:read", read_handler),
        mcp_server.ToolSpec("cancel_thing", "cancel", {"type": "object"}, {"destructiveHint": True}, "runner:cancel", cancel_handler),
    ]


class Running:
    def __init__(self, httpd, thread, logs, oauth_server, store):
        self.httpd, self.thread, self.logs, self.oauth, self.store = httpd, thread, logs, oauth_server, store
        self.port = httpd.server_address[1]

    def request(self, method, path, *, body=None, headers=None, host=HOST, chunked=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                conn.putheader("Host", host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if chunked:
                conn.putheader("Transfer-Encoding", "chunked")
                conn.endheaders()
                data = body or b""
                for i in range(0, len(data), 8192):
                    piece = data[i:i + 8192]
                    conn.send(b"%x\r\n%s\r\n" % (len(piece), piece))
                conn.send(b"0\r\n\r\n")
            else:
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                if body:
                    conn.send(body)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        finally:
            conn.close()

    def mcp(self, payload, token=None, **kw):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.request("POST", "/mcp", body=json.dumps(payload).encode(), headers=headers, **kw)


def _start(steward_home, tmp_path, record, *, public=True, trust_forwarded=True, clock=None):
    logs: list[dict] = []
    store = OAuthStore(tmp_path / "oauth.db", clock=clock) if public else None
    oauth_server = oauth.OAuthServer(store, base_url=BASE, passphrase_record=record) if public else None
    server = mcp_server.MCPServer(_tools(), home=steward_home)
    httpd = mcp_server.make_http_server(
        server, port=0, oauth=oauth_server, trust_forwarded=trust_forwarded, access_log=logs.append
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return Running(httpd, thread, logs, oauth_server, store)


def _stop(running):
    running.httpd.shutdown()
    running.thread.join(timeout=5)
    running.httpd.server_close()
    if running.store is not None:
        running.store.close()


@pytest.fixture
def public(steward_home, tmp_path, record):
    r = _start(steward_home, tmp_path, record)
    yield r
    _stop(r)


@pytest.fixture
def local(steward_home, tmp_path, record):
    r = _start(steward_home, tmp_path, record, public=False)
    yield r
    _stop(r)


def hdr(headers, name):
    for k, v in headers:
        if k.lower() == name.lower():
            return v
    return None


# -- OAuth flow over real HTTP ---------------------------------------------------


def _pkce():
    verifier = base64.urlsafe_b64encode(b"q" * 48).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def oauth_login(r, *, ip="198.51.100.7"):
    fwd = {"CF-Connecting-IP": ip}
    status, _, body = r.request(
        "POST", "/register", headers={"Content-Type": "application/json", **fwd},
        body=json.dumps({"client_name": "ChatGPT", "redirect_uris": [CALLBACK],
                         "token_endpoint_auth_method": "none"}).encode(),
    )
    assert status == 201, body
    cid = json.loads(body)["client_id"]
    verifier, challenge = _pkce()
    q = urlencode({"response_type": "code", "client_id": cid, "redirect_uri": CALLBACK,
                   "code_challenge": challenge, "code_challenge_method": "S256",
                   "state": "s1", "resource": BASE + "/mcp"})
    status, headers, body = r.request("GET", "/authorize?" + q, headers=fwd)
    assert status == 200, body
    page = body.decode()
    tx = re.search(r'name="tx" value="([^"]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    cookie = hdr(headers, "Set-Cookie").split(";", 1)[0]
    status, headers, _ = r.request(
        "POST", "/consent",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE, "Cookie": cookie, **fwd},
        body=urlencode({"tx": tx, "csrf": csrf, "passphrase": PASSPHRASE, "decision": "approve"}).encode(),
    )
    assert status == 302
    code = parse_qs(urlsplit(hdr(headers, "Location")).query)["code"][0]
    status, _, body = r.request(
        "POST", "/token", headers={"Content-Type": "application/x-www-form-urlencoded", **fwd},
        body=urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                        "client_id": cid, "redirect_uri": CALLBACK, "resource": BASE + "/mcp"}).encode(),
    )
    assert status == 200, body
    return json.loads(body)


def test_full_oauth_flow_then_mcp_with_default_grant(public):
    tokens = oauth_login(public)
    status, _, body = public.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, tokens["access_token"])
    assert status == 200 and json.loads(body)["result"]["serverInfo"]["name"] == "steward"
    status, _, body = public.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tokens["access_token"])
    assert {t["name"] for t in json.loads(body)["result"]["tools"]} == {"read_thing", "cancel_thing"}
    status, _, body = public.mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "read_thing", "arguments": {}}}, tokens["access_token"])
    assert json.loads(body)["result"]["structuredContent"]["ok"] is True
    # default consent grant excludes runner:cancel
    status, _, body = public.mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "cancel_thing", "arguments": {}}}, tokens["access_token"])
    assert json.loads(body)["error"]["message"] == "insufficient_scope"


def test_static_bearer_tokens_still_work_publicly(public, steward_home):
    plaintext, _ = mcp_auth.create_token(["runner:read"], home=steward_home)
    status, _, body = public.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, plaintext)
    assert status == 200 and json.loads(body)["result"] == {}


# -- 401 challenge -------------------------------------------------------------------


def test_unauthenticated_post_mcp_is_401_with_resource_metadata(public):
    status, headers, body = public.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert status == 401
    assert hdr(headers, "WWW-Authenticate") == PRM


def test_unauthenticated_even_with_garbage_body_is_401(public):
    status, headers, _ = public.request("POST", "/mcp", body=b"not json", headers={"Content-Type": "application/json"})
    assert status == 401 and hdr(headers, "WWW-Authenticate") == PRM


def test_bad_token_is_401_invalid_token(public):
    status, headers, _ = public.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, "garbage")
    assert status == 401
    assert 'error="invalid_token"' in hdr(headers, "WWW-Authenticate")
    assert "resource_metadata=" in hdr(headers, "WWW-Authenticate")


def test_get_mcp_unauthenticated_is_401_authenticated_is_405(public, steward_home):
    status, headers, _ = public.request("GET", "/mcp")
    assert status == 401 and hdr(headers, "WWW-Authenticate") == PRM
    plaintext, _ = mcp_auth.create_token(["runner:read"], home=steward_home)
    status, _, _ = public.request("GET", "/mcp", headers={"Authorization": f"Bearer {plaintext}"})
    assert status == 405


def test_expired_oauth_token_is_401(steward_home, tmp_path, record):
    class Clock:
        t = 1_800_000_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    r = _start(steward_home, tmp_path, record, clock=clock)
    try:
        tokens = oauth_login(r)
        clock.t += 3601
        status, _, _ = r.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, tokens["access_token"])
        assert status == 401
    finally:
        _stop(r)


# -- route allowlist ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/random", "/mcp/", "/MCP", "/.well-known/openid-configuration",
                                  "/.well-known/oauth-protected-resource", "/../etc/passwd", "/healthz/x"])
def test_paths_outside_allowlist_are_404(public, path):
    assert public.request("GET", path)[0] == 404
    assert public.request("POST", path, body=b"{}")[0] == 404


def test_discovery_documents_are_served(public):
    status, _, body = public.request("GET", "/.well-known/oauth-authorization-server")
    assert status == 200 and json.loads(body)["issuer"] == BASE + "/"
    status, _, body = public.request("GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200 and json.loads(body)["resource"] == BASE + "/mcp"


def test_healthz(public):
    status, _, body = public.request("GET", "/healthz")
    assert status == 200 and json.loads(body) == {"ok": True}


def test_wrong_method_on_allowlisted_path_is_405(public):
    assert public.request("DELETE", "/token")[0] == 405
    assert public.request("PUT", "/healthz", body=b"")[0] == 405


def test_local_mode_does_not_serve_oauth(local):
    assert local.request("GET", "/.well-known/oauth-authorization-server", host=f"127.0.0.1:{local.port}")[0] == 404
    assert local.request("GET", "/healthz", host=f"127.0.0.1:{local.port}")[0] == 200


# -- Host header -------------------------------------------------------------------------------


def test_wrong_host_is_421(public):
    assert public.request("GET", "/healthz", host="evil.example")[0] == 421
    assert public.request("GET", "/healthz", host="example.test.evil.example")[0] == 421
    assert public.request("GET", "/healthz", host=None)[0] == 421
    assert public.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, host="evil.example")[0] == 421


def test_loopback_host_is_accepted(public):
    assert public.request("GET", "/healthz", host=f"127.0.0.1:{public.port}")[0] == 200
    assert public.request("GET", "/healthz", host="EXAMPLE.TEST")[0] == 200


def test_local_mode_rejects_non_loopback_host(local):
    assert local.request("GET", "/healthz", host="evil.example")[0] == 421


# -- body cap ------------------------------------------------------------------------------------


def test_body_over_64kb_is_413(public):
    big = b"x" * (64 * 1024 + 1)
    assert public.request("POST", "/mcp", body=big, headers={"Content-Type": "application/json"})[0] == 413
    assert public.request("POST", "/register", body=big, headers={"Content-Type": "application/json"})[0] == 413


def test_chunked_body_over_64kb_is_413(public):
    big = b"x" * (64 * 1024 + 1)
    assert public.request("POST", "/mcp", body=big, chunked=True)[0] == 413


def test_chunked_small_body_is_accepted(public, steward_home):
    plaintext, _ = mcp_auth.create_token(["runner:read"], home=steward_home)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    status, _, resp = public.request("POST", "/mcp", body=body, chunked=True,
                                     headers={"Authorization": f"Bearer {plaintext}"})
    assert status == 200 and json.loads(resp)["result"] == {}


def test_body_at_64kb_is_not_413(public):
    body = b" " * (64 * 1024)
    status, _, _ = public.request("POST", "/mcp", body=body)
    assert status == 401


# -- rate limits --------------------------------------------------------------------------------


def _register(r, ip):
    return r.request("POST", "/register", headers={"Content-Type": "application/json", "CF-Connecting-IP": ip},
                     body=json.dumps({"redirect_uris": [CALLBACK]}).encode())[0]


def test_register_rate_limited_per_client_ip(public):
    statuses = [_register(public, "203.0.113.1") for _ in range(11)]
    assert statuses[:10] == [201] * 10
    assert statuses[10] == 429
    assert _register(public, "203.0.113.2") == 201


def test_token_and_consent_are_rate_limited(public):
    for path in ("/token", "/consent"):
        statuses = [public.request("POST", path, headers={"CF-Connecting-IP": "203.0.113.9",
                                                          "Content-Type": "application/x-www-form-urlencoded"},
                                   body=b"a=b")[0] for _ in range(11)]
        assert 429 not in statuses[:10] and statuses[10] == 429


def test_forwarded_ip_ignored_unless_trusted(steward_home, tmp_path, record):
    r = _start(steward_home, tmp_path, record, trust_forwarded=False)
    try:
        statuses = [_register(r, f"203.0.113.{i}") for i in range(11)]
        assert statuses[10] == 429  # spoofed header didn't buy a fresh bucket
    finally:
        _stop(r)


# -- access log -----------------------------------------------------------------------------------


def test_access_log_is_structured_and_secret_free(public):
    tokens = oauth_login(public)
    public.mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"}, tokens["access_token"])
    public.request("GET", "/random?secret=abc")
    deadline = time.monotonic() + 3  # the server logs after it replies: wait for the last line instead of racing it
    # each request is logged by its own handler thread after replying, so the /mcp line can land after /random's
    while not (any(l.get("path") == "/random" for l in public.logs)
               and any(l.get("path") == "/mcp" and l.get("status") == 200 for l in public.logs)) \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    logs = list(public.logs)
    assert logs, "no access log lines"
    for line in logs:
        assert {"method", "path", "status", "ms"} <= set(line)
        assert "?" not in line["path"]
    dumped = json.dumps(logs)
    for secret in (tokens["access_token"], tokens["refresh_token"], PASSPHRASE, "secret=abc"):
        assert secret not in dumped
    mcp_lines = [l for l in logs if l["path"] == "/mcp" and l["status"] == 200]
    assert mcp_lines and mcp_lines[-1]["tid"]
    assert len(mcp_lines[-1]["tid"]) <= 11
    assert mcp_lines[-1]["tid"] not in tokens["access_token"]


def test_default_access_log_goes_to_stderr(steward_home, capsys):
    server = mcp_server.MCPServer(_tools(), home=steward_home)
    httpd = mcp_server.make_http_server(server, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/healthz")
        conn.getresponse().read()
        conn.close()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    err = capsys.readouterr().err
    line = json.loads(err.strip().splitlines()[-1])
    assert line["path"] == "/healthz" and line["status"] == 200


# -- serve() ----------------------------------------------------------------------------------------


def test_serve_builds_public_server_and_stops(steward_home, tmp_path, record):
    server = mcp_server.MCPServer(_tools(), home=steward_home)
    started = threading.Event()
    box = {}

    def ready(httpd):
        box["httpd"] = httpd
        started.set()

    def run():
        try:
            mcp_server.serve(server, port=0, public_base_url=BASE, trust_forwarded=True,
                             passphrase_record=record, oauth_home=tmp_path, ready=ready,
                             access_log=lambda line: None)
        except BaseException as exc:  # surface startup errors instead of a bare wait timeout
            box["error"] = exc
            started.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Startup opens a fresh oauth.db (WAL + schema); under -n 8 load that can exceed 5 s.
    assert started.wait(60)
    assert "error" not in box, box.get("error")
    httpd = box["httpd"]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.putrequest("GET", "/.well-known/oauth-protected-resource/mcp", skip_host=True)
        conn.putheader("Host", HOST)
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()
        assert (tmp_path / "oauth.db").exists()
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def test_serve_public_requires_passphrase_record(steward_home):
    server = mcp_server.MCPServer(_tools(), home=steward_home)
    with pytest.raises(ValueError):
        mcp_server.serve(server, port=0, public_base_url=BASE, passphrase_record=None)


def test_still_refuses_non_loopback_bind(steward_home):
    server = mcp_server.MCPServer(_tools(), home=steward_home)
    with pytest.raises(ValueError):
        mcp_server.make_http_server(server, host="0.0.0.0", port=0)


def test_handler_has_socket_timeout(public):
    assert 0 < public.httpd.RequestHandlerClass.timeout <= 60


def test_internal_error_is_500_with_type_only_in_log(public, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-bearing message")

    monkeypatch.setattr(public.oauth, "handle", boom)
    status, _, body = public.request("GET", "/.well-known/oauth-authorization-server")
    assert status == 500 and b"secret" not in body
    # The access-log line is written after the response is flushed; wait for it
    # rather than racing the handler thread.
    import time
    deadline = time.monotonic() + 2
    while not any("err" in l for l in public.logs) and time.monotonic() < deadline:
        time.sleep(0.01)
    line = next(l for l in reversed(public.logs) if "err" in l)
    assert line["err"] == "RuntimeError" and "secret" not in json.dumps(line)
