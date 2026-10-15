"""OAuth 2.1 authorization server handlers (LIVE-01, LIVE-02).

Transport-agnostic: every handler is driven directly through
`OAuthServer.handle(method, path, query, headers, body)` with an injected
clock — no sockets, no network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from steward import oauth
from steward.oauth import OAuthServer, InvalidToken
from steward.oauth_store import OAuthStore

BASE = "https://example.test"
ISSUER = "https://example.test/"
RESOURCE = "https://example.test/mcp"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
PASSPHRASE = "correct horse battery staple"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(scope="module")
def passphrase_record():
    return oauth.hash_passphrase(PASSPHRASE)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = OAuthStore(tmp_path / "oauth.db", clock=clock)
    yield s
    s.close()


@pytest.fixture
def server(store, passphrase_record):
    return OAuthServer(store, base_url=BASE, passphrase_record=passphrase_record)


# -- helpers ---------------------------------------------------------------


def hdr(headers, name):
    vals = [v for k, v in headers if k.lower() == name.lower()]
    return vals[0] if vals else None


def hdrs(headers, name):
    return [v for k, v in headers if k.lower() == name.lower()]


def call(server, method, path, *, query="", headers=None, body=b""):
    return server.handle(method, path, query, {k.lower(): v for k, v in (headers or {}).items()}, body)


def pkce():
    verifier = base64.urlsafe_b64encode(b"v" * 40).decode().rstrip("=") + "abc"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(server, **overrides):
    doc = {
        "client_name": "ChatGPT",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    doc.update(overrides)
    doc = {k: v for k, v in doc.items() if v is not None}
    return call(server, "POST", "/register", headers={"Content-Type": "application/json"},
                body=json.dumps(doc).encode())


def client_id(server):
    status, _, body = register(server)
    assert status == 201, body
    return json.loads(body)["client_id"]


def authorize(server, cid, challenge, **overrides):
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-123",
        "resource": RESOURCE,
    }
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return call(server, "GET", "/authorize", query=urlencode(params))


def consent_inputs(auth_response):
    status, headers, body = auth_response
    assert status == 200, body
    html = body.decode()
    tx = re.search(r'name="tx" value="([^"]+)"', html).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    cookie = hdr(headers, "Set-Cookie").split(";", 1)[0]
    return tx, csrf, cookie


def consent(server, tx, csrf, cookie, passphrase=PASSPHRASE, origin=BASE, decision="approve"):
    fields = {"tx": tx, "csrf": csrf, "passphrase": passphrase, "decision": decision}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    if cookie is not None:
        headers["Cookie"] = cookie
    return call(server, "POST", "/consent", headers=headers, body=urlencode(fields).encode())


def get_code(server, cid=None, **auth_overrides):
    cid = cid or client_id(server)
    verifier, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge, **auth_overrides))
    status, headers, _ = consent(server, tx, csrf, cookie)
    assert status == 302
    q = parse_qs(urlsplit(hdr(headers, "Location")).query)
    return cid, verifier, q["code"][0]


def token(server, fields):
    return call(server, "POST", "/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=urlencode(fields).encode())


def exchange(server, cid, verifier, code, **extra):
    fields = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
              "client_id": cid, "redirect_uri": CALLBACK, "resource": RESOURCE}
    fields.update(extra)
    fields = {k: v for k, v in fields.items() if v is not None}
    return token(server, fields)


def full_tokens(server):
    cid, verifier, code = get_code(server)
    status, headers, body = exchange(server, cid, verifier, code)
    assert status == 200, body
    return cid, json.loads(body)


# -- metadata ----------------------------------------------------------------


def test_authorization_server_metadata(server):
    status, headers, body = call(server, "GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    doc = json.loads(body)
    assert doc["issuer"] == ISSUER
    assert doc["authorization_endpoint"] == BASE + "/authorize"
    assert doc["token_endpoint"] == BASE + "/token"
    assert doc["registration_endpoint"] == BASE + "/register"
    assert doc["revocation_endpoint"] == BASE + "/revoke"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert doc["response_types_supported"] == ["code"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["authorization_response_iss_parameter_supported"] is True
    assert set(doc["scopes_supported"]) == {"runner:read", "runner:steer", "runner:pause"}


def test_protected_resource_metadata(server):
    status, _, body = call(server, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200
    doc = json.loads(body)
    assert doc["resource"] == RESOURCE
    assert doc["authorization_servers"] == [ISSUER]
    assert "runner:read" in doc["scopes_supported"]


def test_www_authenticate_names_resource_metadata(server):
    assert server.www_authenticate() == (
        'Bearer resource_metadata="https://example.test/.well-known/oauth-protected-resource/mcp"'
    )
    assert 'error="invalid_token"' in server.www_authenticate(error="invalid_token")


def test_base_url_must_be_https_origin(store, passphrase_record):
    for bad in ("http://example.test", "https://example.test/sub", "https://u:p@example.test",
                "https://example.test/?q=1", "example.test"):
        with pytest.raises(ValueError):
            OAuthServer(store, base_url=bad, passphrase_record=passphrase_record)


def test_admin_scope_can_never_be_granted(store, passphrase_record):
    with pytest.raises(ValueError):
        OAuthServer(store, base_url=BASE, passphrase_record=passphrase_record,
                    grant_scopes=("runner:read", "runner:admin"))


def test_unknown_path_is_not_handled(server):
    assert server.handles("/authorize") and server.handles("/.well-known/oauth-authorization-server")
    assert not server.handles("/random")
    assert not server.handles("/mcp")


# -- dynamic client registration ------------------------------------------------


def test_register_public_client(server):
    status, headers, body = register(server)
    assert status == 201
    doc = json.loads(body)
    assert doc["client_id"]
    assert doc["redirect_uris"] == [CALLBACK]
    assert doc["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in doc
    assert hdr(headers, "Cache-Control") == "no-store"


@pytest.mark.parametrize(
    "overrides",
    [
        {"redirect_uris": ["http://chatgpt.com/cb"]},
        {"redirect_uris": []},
        {"redirect_uris": [CALLBACK + str(i) for i in range(6)]},
        {"redirect_uris": ["https://evil.example/cb"]},
        {"redirect_uris": ["https://user:pw@chatgpt.com/cb"]},
        {"redirect_uris": ["https://chatgpt.com/cb#frag"]},
        {"redirect_uris": "https://chatgpt.com/cb"},
        {"client_name": "x" * 101},
        {"token_endpoint_auth_method": "client_secret_post"},
        {"grant_types": ["client_credentials"]},
        {"response_types": ["token"]},
        {"scope": "runner:read bogus:scope"},
    ],
)
def test_register_rejects_bad_metadata(server, overrides):
    status, _, body = register(server, **overrides)
    assert status == 400
    assert json.loads(body)["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}


def test_register_rejects_non_json(server):
    status, _, _ = call(server, "POST", "/register", headers={"Content-Type": "application/json"}, body=b"nope")
    assert status == 400


def test_redirect_host_allowlist_is_configurable(store, passphrase_record):
    s = OAuthServer(store, base_url=BASE, passphrase_record=passphrase_record,
                    redirect_hosts=("client.example",))
    status, _, _ = register(s, redirect_uris=["https://client.example/cb"])
    assert status == 201


# -- /authorize --------------------------------------------------------------------


def test_authorize_unknown_client_does_not_redirect(server):
    _, challenge = pkce()
    status, headers, _ = authorize(server, "nope", challenge)
    assert status == 400
    assert hdr(headers, "Location") is None


def test_authorize_redirect_uri_must_match_exactly(server):
    cid = client_id(server)
    _, challenge = pkce()
    for bad in (CALLBACK + "/", CALLBACK + "?x=1", "https://chatgpt.com/other"):
        status, headers, _ = authorize(server, cid, challenge, redirect_uri=bad)
        assert status == 400
        assert hdr(headers, "Location") is None


def _redirect_error(response):
    status, headers, _ = response
    assert status == 302
    loc = urlsplit(hdr(headers, "Location"))
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CALLBACK
    q = parse_qs(loc.query)
    assert q["iss"] == [ISSUER]
    return q


def test_authorize_requires_s256_pkce(server):
    cid = client_id(server)
    _, challenge = pkce()
    assert _redirect_error(authorize(server, cid, None))["error"] == ["invalid_request"]
    assert _redirect_error(authorize(server, cid, challenge, code_challenge_method="plain"))["error"] == ["invalid_request"]
    assert _redirect_error(authorize(server, cid, challenge, code_challenge_method=None))["error"] == ["invalid_request"]
    assert _redirect_error(authorize(server, cid, "short"))["error"] == ["invalid_request"]


def test_authorize_response_type_code_only(server):
    cid = client_id(server)
    _, challenge = pkce()
    q = _redirect_error(authorize(server, cid, challenge, response_type="token"))
    assert q["error"] == ["unsupported_response_type"]
    assert q["state"] == ["st-123"]


def test_authorize_resource_must_be_exact(server):
    cid = client_id(server)
    _, challenge = pkce()
    for bad in ("https://example.test/mcp/", "https://other.test/mcp", "https://example.test/"):
        assert _redirect_error(authorize(server, cid, challenge, resource=bad))["error"] == ["invalid_target"]


def test_authorize_unknown_scope_rejected(server):
    cid = client_id(server)
    _, challenge = pkce()
    assert _redirect_error(authorize(server, cid, challenge, scope="runner:read evil"))["error"] == ["invalid_scope"]


def test_authorize_renders_consent_page_with_csrf_cookie(server):
    cid = client_id(server)
    _, challenge = pkce()
    status, headers, body = authorize(server, cid, challenge)
    assert status == 200
    html = body.decode()
    assert "ChatGPT" in html and "runner:read" in html and "runner:steer" in html and "runner:pause" in html
    assert "chatgpt.com" in html
    assert 'type="password"' in html
    cookie = hdr(headers, "Set-Cookie")
    assert cookie.startswith("__Host-")
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in cookie
    csp = hdr(headers, "Content-Security-Policy")
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self' https://chatgpt.com" in csp
    assert hdr(headers, "X-Frame-Options") == "DENY"
    assert hdr(headers, "Cache-Control") == "no-store"
    assert hdr(headers, "Referrer-Policy") == "strict-origin"


def test_consent_page_escapes_client_name(server):
    status, _, body = register(server, client_name="<script>alert(1)</script>")
    cid = json.loads(body)["client_id"]
    _, challenge = pkce()
    _, _, page = authorize(server, cid, challenge)
    assert b"<script>" not in page
    assert b"&lt;script&gt;" in page


def test_default_grant_is_read_steer_pause_even_if_more_requested(server):
    cid = client_id(server)
    verifier, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(
        server, cid, challenge, scope="runner:read runner:steer runner:pause runner:cancel runner:approve"))
    status, headers, _ = consent(server, tx, csrf, cookie)
    code = parse_qs(urlsplit(hdr(headers, "Location")).query)["code"][0]
    status, _, body = exchange(server, cid, verifier, code)
    doc = json.loads(body)
    assert doc["scope"].split() == ["runner:read", "runner:steer", "runner:pause"]
    assert set(server.validate(doc["access_token"]).scopes) == {"runner:read", "runner:steer", "runner:pause"}


def test_no_scope_requested_gets_default_grant(server):
    cid, doc = full_tokens(server)
    assert doc["scope"].split() == ["runner:read", "runner:steer", "runner:pause"]


def test_narrower_scope_request_is_honoured(server):
    cid, verifier, code = get_code(server, scope="runner:read")
    _, _, body = exchange(server, cid, verifier, code)
    assert json.loads(body)["scope"] == "runner:read"


# -- /consent -------------------------------------------------------------------------


def _code_rows(store):
    return store._conn.execute("SELECT count(*) FROM auth_codes").fetchone()[0]


def test_wrong_passphrase_issues_nothing(server, store):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    status, headers, body = consent(server, tx, csrf, cookie, passphrase="wrong")
    assert status == 403
    assert hdr(headers, "Location") is None
    assert _code_rows(store) == 0
    # the transaction is still usable with the right passphrase
    status, _, _ = consent(server, tx, csrf, cookie)
    assert status == 302


def test_repeated_wrong_passphrase_burns_transaction(server, store):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    for _ in range(oauth.MAX_PASSPHRASE_ATTEMPTS):
        assert consent(server, tx, csrf, cookie, passphrase="wrong")[0] == 403
    status, _, _ = consent(server, tx, csrf, cookie)
    assert status == 400
    assert _code_rows(store) == 0


def test_consent_requires_csrf_cookie(server, store):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    assert consent(server, tx, csrf, None)[0] == 403
    name = cookie.split("=", 1)[0]
    assert consent(server, tx, csrf, f"{name}=forged")[0] == 403
    assert consent(server, tx, "forged", f"{name}=forged")[0] == 403
    assert _code_rows(store) == 0


def test_consent_cookie_is_per_transaction(server):
    cid = client_id(server)
    _, c1 = pkce()
    tx1, csrf1, cookie1 = consent_inputs(authorize(server, cid, c1))
    tx2, csrf2, cookie2 = consent_inputs(authorize(server, cid, c1))
    assert cookie1.split("=")[0] != cookie2.split("=")[0]
    # a second tab doesn't invalidate the first; both cookies present
    both = f"{cookie1}; {cookie2}"
    assert consent(server, tx1, csrf1, both)[0] == 302
    assert consent(server, tx2, csrf2, both)[0] == 302
    # one tab's cookie does not authorize the other transaction
    tx3, csrf3, _ = consent_inputs(authorize(server, cid, c1))
    assert consent(server, tx3, csrf3, cookie1)[0] == 403


def test_consent_requires_same_origin(server, store):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    assert consent(server, tx, csrf, cookie, origin=None)[0] == 403
    assert consent(server, tx, csrf, cookie, origin="https://evil.example")[0] == 403
    assert consent(server, tx, csrf, cookie, origin="null")[0] == 403
    assert _code_rows(store) == 0


def test_consent_success_redirects_with_code_state_iss(server):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    status, headers, _ = consent(server, tx, csrf, cookie)
    assert status == 302
    loc = urlsplit(hdr(headers, "Location"))
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CALLBACK
    q = parse_qs(loc.query)
    assert q["state"] == ["st-123"] and q["iss"] == [ISSUER] and q["code"][0]
    # the CSRF cookie is cleared
    assert any("Max-Age=0" in c for c in hdrs(headers, "Set-Cookie"))
    # the transaction is single-use
    assert consent(server, tx, csrf, cookie)[0] == 400


def test_consent_deny_redirects_access_denied(server, store):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    status, headers, _ = consent(server, tx, csrf, cookie, passphrase="", decision="deny")
    assert status == 302
    q = parse_qs(urlsplit(hdr(headers, "Location")).query)
    assert q["error"] == ["access_denied"]
    assert _code_rows(store) == 0


def test_consent_transaction_expires_after_10_minutes(server, clock):
    cid = client_id(server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(server, cid, challenge))
    clock.advance(601)
    assert consent(server, tx, csrf, cookie)[0] == 400


def test_consent_rejects_get(server):
    status, _, _ = call(server, "GET", "/consent")
    assert status == 405


# -- /token -------------------------------------------------------------------------------


def test_token_exchange_success(server):
    cid, verifier, code = get_code(server)
    status, headers, body = exchange(server, cid, verifier, code)
    assert status == 200
    assert hdr(headers, "Cache-Control") == "no-store"
    doc = json.loads(body)
    assert doc["token_type"] == "Bearer"
    assert doc["expires_in"] == 3600
    assert doc["access_token"] and doc["refresh_token"]
    grant = server.validate(doc["access_token"], RESOURCE)
    assert grant.client_id == cid


def test_code_is_single_use_and_replay_revokes_tokens(server):
    cid, verifier, code = get_code(server)
    _, _, body = exchange(server, cid, verifier, code)
    first = json.loads(body)
    status, _, body = exchange(server, cid, verifier, code)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"
    with pytest.raises(InvalidToken):
        server.validate(first["access_token"])


def test_code_expires_after_2_minutes(server, clock):
    cid, verifier, code = get_code(server)
    clock.advance(121)
    status, _, body = exchange(server, cid, verifier, code)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_code_within_2_minutes_works(server, clock):
    cid, verifier, code = get_code(server)
    clock.advance(119)
    assert exchange(server, cid, verifier, code)[0] == 200


def test_pkce_verifier_must_match(server):
    cid, verifier, code = get_code(server)
    status, _, body = exchange(server, cid, "x" * 43, code)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"
    status, _, _ = exchange(server, cid, None, code)
    assert status == 400


def test_code_bound_to_client_and_redirect(server):
    cid, verifier, code = get_code(server)
    other = client_id(server)
    assert exchange(server, other, verifier, code)[0] == 400
    cid2, verifier2, code2 = get_code(server)
    assert exchange(server, cid2, verifier2, code2, redirect_uri="https://chatgpt.com/other")[0] == 400


def test_token_resource_must_match(server):
    cid, verifier, code = get_code(server)
    status, _, body = exchange(server, cid, verifier, code, resource="https://other.test/mcp")
    assert status == 400 and json.loads(body)["error"] == "invalid_target"


def test_unknown_client_is_invalid_client(server):
    status, _, body = token(server, {"grant_type": "authorization_code", "client_id": "nope",
                                     "code": "x", "code_verifier": "y" * 43})
    assert status == 401 and json.loads(body)["error"] == "invalid_client"


def test_unsupported_grant_type(server):
    cid = client_id(server)
    status, _, body = token(server, {"grant_type": "password", "client_id": cid})
    assert status == 400 and json.loads(body)["error"] == "unsupported_grant_type"


def test_token_requires_form_encoding(server):
    status, _, _ = call(server, "POST", "/token", headers={"Content-Type": "application/json"}, body=b"{}")
    assert status == 400


# -- access tokens ---------------------------------------------------------------------------


def test_access_token_expires_after_1_hour(server, clock):
    _, doc = full_tokens(server)
    clock.advance(3599)
    server.validate(doc["access_token"])
    clock.advance(2)
    with pytest.raises(InvalidToken):
        server.validate(doc["access_token"])


def test_access_token_bound_to_exact_resource(server, store, passphrase_record):
    _, doc = full_tokens(server)
    with pytest.raises(InvalidToken):
        server.validate(doc["access_token"], "https://example.test/other")
    other = OAuthServer(store, base_url="https://other.test", passphrase_record=passphrase_record)
    with pytest.raises(InvalidToken):
        other.validate(doc["access_token"])


def test_garbage_and_refresh_tokens_are_not_access_tokens(server):
    _, doc = full_tokens(server)
    for bad in ("", "garbage", doc["refresh_token"]):
        with pytest.raises(InvalidToken):
            server.validate(bad)


# -- refresh --------------------------------------------------------------------------------


def refresh(server, cid, rt, **extra):
    fields = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}
    fields.update(extra)
    return token(server, fields)


def test_refresh_rotates(server):
    cid, doc = full_tokens(server)
    status, _, body = refresh(server, cid, doc["refresh_token"])
    assert status == 200
    new = json.loads(body)
    assert new["refresh_token"] != doc["refresh_token"]
    assert new["access_token"] != doc["access_token"]
    server.validate(new["access_token"])
    with pytest.raises(InvalidToken):  # superseded access token retired on rotation
        server.validate(doc["access_token"])


def test_refresh_reuse_revokes_whole_family(server):
    cid, doc = full_tokens(server)
    _, _, body = refresh(server, cid, doc["refresh_token"])
    new = json.loads(body)
    status, _, body = refresh(server, cid, doc["refresh_token"])  # replay the rotated token
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"
    with pytest.raises(InvalidToken):
        server.validate(new["access_token"])
    status, _, _ = refresh(server, cid, new["refresh_token"])
    assert status == 400


def test_refresh_bound_to_client(server):
    cid, doc = full_tokens(server)
    other = client_id(server)
    assert refresh(server, other, doc["refresh_token"])[0] == 400
    assert refresh(server, cid, doc["refresh_token"])[0] == 200


def test_refresh_cannot_widen_scope(server):
    cid, doc = full_tokens(server)
    status, _, body = refresh(server, cid, doc["refresh_token"], scope="runner:read runner:cancel")
    assert status == 400 and json.loads(body)["error"] == "invalid_scope"
    status, _, body = refresh(server, cid, doc["refresh_token"], scope="runner:read")
    assert status == 200 and json.loads(body)["scope"] == "runner:read"


def test_refresh_token_expires_after_30_days(server, clock):
    cid, doc = full_tokens(server)
    clock.advance(30 * 86400 + 1)
    assert refresh(server, cid, doc["refresh_token"])[0] == 400


# -- /revoke --------------------------------------------------------------------------------


def test_revoke_refresh_kills_family(server):
    cid, doc = full_tokens(server)
    status, _, _ = call(server, "POST", "/revoke",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=urlencode({"token": doc["refresh_token"], "client_id": cid}).encode())
    assert status == 200
    with pytest.raises(InvalidToken):
        server.validate(doc["access_token"])


def test_revoke_unknown_token_is_200(server):
    status, _, _ = call(server, "POST", "/revoke",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=urlencode({"token": "whatever"}).encode())
    assert status == 200


# -- secrets at rest --------------------------------------------------------------------------


def test_no_plaintext_token_or_passphrase_on_disk(server, store, tmp_path):
    cid, doc = full_tokens(server)
    _, _, body = refresh(server, cid, doc["refresh_token"])
    new = json.loads(body)
    store._conn.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    for secret in (doc["access_token"], doc["refresh_token"], new["access_token"], new["refresh_token"], PASSPHRASE):
        assert secret.encode() not in raw


def test_passphrase_hash_roundtrip(passphrase_record):
    assert passphrase_record["alg"] == "scrypt"
    assert PASSPHRASE not in json.dumps(passphrase_record)
    assert oauth.verify_passphrase(passphrase_record, PASSPHRASE)
    assert not oauth.verify_passphrase(passphrase_record, PASSPHRASE + "x")
    assert not oauth.verify_passphrase({"alg": "scrypt"}, PASSPHRASE)


def test_errors_never_reflect_input(server):
    evil = "<b>secret-looking-input</b>"
    cid = client_id(server)
    status, _, body = token(server, {"grant_type": "authorization_code", "client_id": cid,
                                     "code": evil, "code_verifier": evil})
    assert evil.encode() not in body


def test_client_cap_evicts_idle_clients_but_never_linked_ones(server, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 3)
    linked, _ = full_tokens(server)  # holds tokens
    idle = [client_id(server), client_id(server)]
    status, _, body = register(server)  # at cap: the oldest idle client is evicted
    assert status == 201
    assert server.store.get_client(linked) is not None
    assert server.store.get_client(idle[0]) is None
    assert server.store.get_client(idle[1]) is not None


def test_client_cap_holds_when_every_client_is_linked(server, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 1)
    full_tokens(server)
    status, _, body = register(server)
    assert status == 400 and json.loads(body)["error"] == "invalid_client_metadata"


BROWSER = "https://steward-auth.tailnet.example.test"


@pytest.fixture
def split_server(store, passphrase_record):
    return OAuthServer(store, base_url=BASE, passphrase_record=passphrase_record, browser_base_url=BROWSER)


def test_browser_origin_split_moves_only_authorize_to_browser_origin(split_server):
    doc = json.loads(call(split_server, "GET", "/.well-known/oauth-authorization-server")[2])
    assert doc["authorization_endpoint"] == BROWSER + "/authorize"
    assert doc["issuer"] == ISSUER
    assert doc["token_endpoint"] == BASE + "/token"
    assert doc["registration_endpoint"] == BASE + "/register"


def test_browser_origin_split_consent_requires_browser_origin(split_server):
    cid = client_id(split_server)
    _, challenge = pkce()
    tx, csrf, cookie = consent_inputs(authorize(split_server, cid, challenge))
    assert consent(split_server, tx, csrf, cookie, origin=BASE)[0] == 403
    tx, csrf, cookie = consent_inputs(authorize(split_server, cid, challenge))
    assert consent(split_server, tx, csrf, cookie, origin=BROWSER)[0] == 302
