"""OAuth 2.1 authorization server for the public Steward MCP connector (LIVE-01, LIVE-02).

Transport-agnostic: `OAuthServer.handle(method, path, query, headers, body)`
returns `(status, headers, body)` where `headers` is a list of
`(name, value)` pairs (Set-Cookie may repeat) and `headers` in is a dict with
lower-cased names. `steward.mcp_server` wires it to HTTP.

Faithful stdlib re-implementation of the ChatGPT account-linking flow that
herdr-gpt proved (`~/projects/herdr-gpt/herdr_gpt/oauth_provider.py`,
`surface_consent.py`, `docs/authentication-procedure.md`; read, not imported):

* RFC 8414 AS metadata + RFC 9728 protected-resource metadata. The issuer is
  serialized exactly as the MCP Python SDK/pydantic does it (`https://host/`)
  and used identically in the AS metadata, the PRM `authorization_servers`,
  and the RFC 9207 `iss` callback parameter.
* RFC 7591 dynamic registration of *public* clients only
  (`token_endpoint_auth_method=none`), https redirect URIs on an allowlisted
  host (default chatgpt.com / chat.openai.com), ≤5 URIs, name ≤100 chars.
* Authorization Code + PKCE S256 only. `/authorize` validates client,
  exact redirect URI, response_type, PKCE, resource and scope, opens a
  10-minute transaction and renders the consent page with a per-transaction
  `__Host-` CSRF cookie (HttpOnly, Secure, SameSite=Lax).
* `/consent` requires same-origin POST (exact Origin), the transaction's CSRF
  cookie == form token == stored digest, and the owner passphrase (scrypt,
  constant-time compare). Wrong passphrase issues nothing; five wrong
  attempts burn the transaction. Success → 302 to the registered callback
  with `code`, `state`, `iss`.
* Codes: 2 minutes, single use; a replayed code revokes the family minted
  from it. Access tokens: 1 hour, bound to the exact `<base>/mcp` resource.
  Refresh tokens: 30 days, rotated on every use; presenting a rotated refresh
  token revokes the whole family.
* Scopes: the grantable set defaults to runner:read runner:steer
  runner:pause (SP-30). A request for more known scopes is *narrowed* to the
  grantable set (the token response's `scope` says what was granted);
  runner:admin can never be granted over OAuth.

Errors carry fixed strings only — no request input is ever reflected, and
nothing here logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from steward import mcp_auth
from steward.oauth_store import OAuthStore, digest

ACCESS_TTL = 3600
CODE_TTL = 120
TX_TTL = 600
REFRESH_TTL = 30 * 86400
MAX_PASSPHRASE_ATTEMPTS = 5
MAX_CLIENTS = 200
MAX_OPEN_TRANSACTIONS = 100

DEFAULT_GRANT_SCOPES = ("runner:read", "runner:steer", "runner:pause")
OAUTH_NEVER_GRANTABLE = frozenset({"runner:admin"})
DEFAULT_REDIRECT_HOSTS = ("chatgpt.com", "chat.openai.com")

AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
ROUTES = frozenset({
    AS_METADATA_PATH,
    PRM_PATH,
    "/register",
    "/authorize",
    "/consent",
    "/token",
    "/revoke",
})

SCOPE_DESCRIPTIONS = {
    "runner:read": "Read job status, events, verification results and diffs",
    "runner:steer": "Send steering instructions to running jobs",
    "runner:pause": "Pause and resume jobs",
    "runner:cancel": "Cancel jobs",
    "runner:approve": "Approve or deny pending human-judgment approvals",
}

_PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_TX_ID = re.compile(r"[0-9a-f]{32}\Z")
_COOKIE_PREFIX = "__Host-steward-csrf-"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32

Response = tuple[int, list[tuple[str, str]], bytes]


class InvalidToken(Exception):
    """Access token unknown, revoked, expired, or bound to another resource."""


@dataclass(frozen=True)
class AccessGrant:
    token_id: str
    client_id: str
    scopes: tuple[str, ...]


# -- owner passphrase (scrypt) -------------------------------------------------


def hash_passphrase(passphrase: str, *, salt: bytes | None = None) -> dict:
    """Salted scrypt record; the only persisted form of the owner passphrase."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )
    return {
        "alg": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": salt.hex(),
        "hash": derived.hex(),
    }


def verify_passphrase(record: dict, passphrase: str) -> bool:
    try:
        if record.get("alg") != "scrypt" or not 1 <= len(passphrase) <= 4096:
            return False
        expected = bytes.fromhex(record["hash"])
        candidate = hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=bytes.fromhex(record["salt"]),
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
            dklen=len(expected),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


# -- helpers --------------------------------------------------------------------


def _pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def _unique_params(raw: str, *, max_fields: int = 20) -> dict[str, str] | None:
    """Parse a query/form string; None on malformed input or duplicated keys."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=max_fields)
    except ValueError:
        return None
    out: dict[str, str] = {}
    for key, value in pairs:
        if key in out:
            return None
        out[key] = value
    return out


def _form(headers: dict, body: bytes) -> dict[str, str] | None:
    ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _unique_params(text)


def _cookies(headers: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in headers.get("cookie", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name not in out:
            out[name] = value.strip()
    return out


def _json(status: int, doc: dict, extra: Iterable[tuple[str, str]] = ()) -> Response:
    headers = [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
        *extra,
    ]
    return status, headers, json.dumps(doc).encode("utf-8")


def _oauth_error(status: int, error: str, description: str | None = None) -> Response:
    doc = {"error": error}
    if description:
        doc["error_description"] = description
    return _json(status, doc)


def _canonical_scopes(scopes: Iterable[str]) -> list[str]:
    wanted = set(scopes)
    return [s for s in mcp_auth.SCOPES if s in wanted]


def normalize_base_url(base_url: str) -> str:
    """Validate a public base URL (https origin, no path/query/credentials);
    return it without a trailing slash."""
    parts = urlsplit(base_url if isinstance(base_url, str) else "")
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("public base URL must be an https origin with no path, e.g. https://host")
    return f"https://{parts.netloc.lower()}"


class OAuthServer:
    def __init__(
        self,
        store: OAuthStore,
        *,
        base_url: str,
        passphrase_record: dict,
        grant_scopes: Iterable[str] = DEFAULT_GRANT_SCOPES,
        redirect_hosts: Iterable[str] = DEFAULT_REDIRECT_HOSTS,
        browser_base_url: str | None = None,
    ):
        parts = urlsplit(normalize_base_url(base_url))
        grant_scopes = tuple(grant_scopes)
        grant = _canonical_scopes(grant_scopes)
        if not grant or set(grant) != set(grant_scopes) or OAUTH_NEVER_GRANTABLE & set(grant):
            raise ValueError("grant scopes must be known runner scopes and never include runner:admin")

        self.store = store
        self.passphrase_record = passphrase_record
        self.grant_scopes = tuple(grant)
        self.redirect_hosts = frozenset(h.lower() for h in redirect_hosts)
        self.origin = f"https://{parts.netloc.lower()}"
        self.host = parts.netloc.lower()
        self.issuer = self.origin + "/"
        self.resource = self.origin + "/mcp"
        self.resource_metadata_url = self.origin + PRM_PATH
        # Only the owner's browser ever loads /authorize and /consent, so they may
        # live on a different (e.g. tailnet-only) origin than the issuer, while
        # ChatGPT's servers keep using the public /register, /token and /mcp.
        bparts = urlsplit(normalize_base_url(browser_base_url)) if browser_base_url else parts
        self.browser_origin = f"https://{bparts.netloc.lower()}"
        self.browser_host = bparts.netloc.lower()

    # -- routing ------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path in ROUTES

    def handle(self, method: str, path: str, query: str, headers: dict, body: bytes) -> Response:
        if path == AS_METADATA_PATH:
            return self._get_only(method, self.authorization_server_metadata)
        if path == PRM_PATH:
            return self._get_only(method, self.protected_resource_metadata)
        if path == "/authorize":
            if method != "GET":
                return self._method_not_allowed("GET")
            return self.authorize(query)
        if path == "/register":
            return self._post_only(method, lambda: self.register(headers, body))
        if path == "/consent":
            return self._post_only(method, lambda: self.consent(headers, body))
        if path == "/token":
            return self._post_only(method, lambda: self.token(headers, body))
        if path == "/revoke":
            return self._post_only(method, lambda: self.revoke(headers, body))
        return 404, [("Content-Type", "application/json")], b'{"error": "not found"}'

    @staticmethod
    def _method_not_allowed(allow: str) -> Response:
        return 405, [("Allow", allow), ("Content-Type", "application/json")], b'{"error": "method not allowed"}'

    def _get_only(self, method, fn) -> Response:
        return fn() if method == "GET" else self._method_not_allowed("GET")

    def _post_only(self, method, fn) -> Response:
        return fn() if method == "POST" else self._method_not_allowed("POST")

    # -- discovery -------------------------------------------------------------------

    def authorization_server_metadata(self) -> Response:
        return _json(200, {
            "issuer": self.issuer,
            "authorization_endpoint": self.browser_origin + "/authorize",
            "token_endpoint": self.origin + "/token",
            "registration_endpoint": self.origin + "/register",
            "revocation_endpoint": self.origin + "/revoke",
            "scopes_supported": list(self.grant_scopes),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        })

    def protected_resource_metadata(self) -> Response:
        return _json(200, {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(self.grant_scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "Steward",
        })

    def www_authenticate(self, error: str | None = None) -> str:
        if error:
            return f'Bearer error="{error}", resource_metadata="{self.resource_metadata_url}"'
        return f'Bearer resource_metadata="{self.resource_metadata_url}"'

    # -- dynamic client registration (RFC 7591) ------------------------------------------

    def _redirect_ok(self, uri: object) -> bool:
        if not isinstance(uri, str) or not 1 <= len(uri) <= 2048:
            return False
        try:
            parts = urlsplit(uri)
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.hostname in self.redirect_hosts
            and port is None
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and "#" not in uri
        )

    def register(self, headers: dict, body: bytes) -> Response:
        try:
            doc = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _oauth_error(400, "invalid_client_metadata", "body must be a JSON object")
        if not isinstance(doc, dict):
            return _oauth_error(400, "invalid_client_metadata", "body must be a JSON object")

        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= 5 or not all(self._redirect_ok(u) for u in uris):
            return _oauth_error(400, "invalid_redirect_uri", "redirect_uris: 1-5 https URIs on an allowed host")
        name = doc.get("client_name", "unnamed client")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            return _oauth_error(400, "invalid_client_metadata", "client_name must be 1-100 characters")
        if doc.get("token_endpoint_auth_method", "none") != "none":
            return _oauth_error(400, "invalid_client_metadata", "only public clients (none) are supported")
        grant_types = doc.get("grant_types", ["authorization_code", "refresh_token"])
        if (
            not isinstance(grant_types, list)
            or "authorization_code" not in grant_types
            or not set(grant_types) <= {"authorization_code", "refresh_token"}
        ):
            return _oauth_error(400, "invalid_client_metadata", "unsupported grant_types")
        response_types = doc.get("response_types", ["code"])
        if response_types != ["code"]:
            return _oauth_error(400, "invalid_client_metadata", "unsupported response_types")
        scope_raw = doc.get("scope")
        if scope_raw is None:
            scopes = list(self.grant_scopes)
        else:
            if not isinstance(scope_raw, str):
                return _oauth_error(400, "invalid_client_metadata", "invalid scope")
            requested = scope_raw.split()
            if any(s not in mcp_auth.SCOPES for s in requested):
                return _oauth_error(400, "invalid_client_metadata", "invalid scope")
            scopes = _canonical_scopes(requested)

        self.store.purge_expired()
        while self.store.count_clients() >= MAX_CLIENTS:
            if not self.store.evict_idle_client():
                return _oauth_error(400, "invalid_client_metadata", "client registration limit reached")

        client_id = "stw_client_" + secrets.token_urlsafe(18)
        self.store.register_client(client_id, name, list(uris), scopes)
        return _json(201, {
            "client_id": client_id,
            "client_id_issued_at": self.store.now(),
            "client_name": name,
            "redirect_uris": list(uris),
            "grant_types": list(grant_types),
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(scopes),
        })

    # -- /authorize --------------------------------------------------------------------------

    def _callback(self, redirect_uri: str, values: dict[str, str]) -> str:
        values = {**values, "iss": self.issuer}
        sep = "&" if urlsplit(redirect_uri).query else "?"
        return redirect_uri + sep + urlencode(values)

    def _redirect(self, location: str, extra: Iterable[tuple[str, str]] = ()) -> Response:
        return 302, [("Location", location), ("Cache-Control", "no-store"), *extra], b""

    def _redirect_error(self, redirect_uri: str, error: str, state: str | None) -> Response:
        values = {"error": error}
        if state is not None:
            values["state"] = state
        return self._redirect(self._callback(redirect_uri, values))

    def _page_headers(self, callback_origin: str | None = None) -> list[tuple[str, str]]:
        form_action = "'self'" + (f" {callback_origin}" if callback_origin else "")
        return [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            # strict-origin (not no-referrer): browsers must still send a real
            # Origin on the same-origin consent POST.
            ("Referrer-Policy", "strict-origin"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            (
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                f"frame-ancestors 'none'; form-action {form_action}",
            ),
        ]

    def _error_page(self, status: int, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Steward</title></head>"
            f"<body><p>{html.escape(message)}</p></body></html>"
        )
        return status, self._page_headers(), body.encode("utf-8")

    def authorize(self, query: str) -> Response:
        params = _unique_params(query)
        if params is None:
            return self._error_page(400, "Invalid authorization request.")
        client = self.store.get_client(params.get("client_id", "")) if params.get("client_id") else None
        if client is None:
            return self._error_page(400, "Unknown client.")
        redirect_uri = params.get("redirect_uri")
        if redirect_uri is None:
            if len(client["redirect_uris"]) != 1:
                return self._error_page(400, "redirect_uri is required.")
            redirect_uri, explicit = client["redirect_uris"][0], False
        else:
            if redirect_uri not in client["redirect_uris"]:
                return self._error_page(400, "redirect_uri does not match the registered client.")
            explicit = True

        # From here on the redirect target is trusted: errors go back to the client.
        state = params.get("state")
        if state is not None and len(state) > 1024:
            return self._redirect_error(redirect_uri, "invalid_request", None)
        if params.get("response_type") != "code":
            return self._redirect_error(redirect_uri, "unsupported_response_type", state)
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not _PKCE_CHALLENGE.fullmatch(challenge):
            return self._redirect_error(redirect_uri, "invalid_request", state)
        resource = params.get("resource")
        if resource is not None and resource != self.resource:
            return self._redirect_error(redirect_uri, "invalid_target", state)
        scope_raw = params.get("scope")
        if scope_raw is None or not scope_raw.strip():
            granted = list(self.grant_scopes)
        else:
            requested = scope_raw.split()
            if any(s not in mcp_auth.SCOPES for s in requested):
                return self._redirect_error(redirect_uri, "invalid_scope", state)
            granted = [s for s in _canonical_scopes(requested) if s in self.grant_scopes]
            if not granted:
                return self._redirect_error(redirect_uri, "invalid_scope", state)

        self.store.purge_expired()
        if self.store.count_open_transactions() >= MAX_OPEN_TRANSACTIONS:
            return self._redirect_error(redirect_uri, "temporarily_unavailable", state)

        tx_id = secrets.token_hex(16)
        csrf = secrets.token_urlsafe(32)
        self.store.create_transaction(
            tx_id=tx_id,
            csrf_digest=digest(csrf),
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            redirect_explicit=explicit,
            code_challenge=challenge,
            scopes=granted,
            state=state,
            resource=self.resource,
            ttl=TX_TTL,
        )
        status, headers, body = self._consent_page(
            200, client_name=client["client_name"], redirect_uri=redirect_uri,
            scopes=granted, tx_id=tx_id, csrf=csrf,
        )
        headers.append((
            "Set-Cookie",
            f"{_COOKIE_PREFIX}{tx_id}={csrf}; Path=/; Max-Age={TX_TTL}; HttpOnly; Secure; SameSite=Lax",
        ))
        return status, headers, body

    def _consent_page(
        self, status: int, *, client_name: str, redirect_uri: str, scopes: list[str],
        tx_id: str, csrf: str, error: str | None = None,
    ) -> Response:
        cb = urlsplit(redirect_uri)
        callback_origin = f"{cb.scheme}://{cb.netloc}"
        esc = html.escape
        scope_items = "".join(
            f"<li><code>{esc(s)}</code> — {esc(SCOPE_DESCRIPTIONS.get(s, s))}</li>" for s in scopes
        )
        err = f'<p class="err">{esc(error)}</p>' if error else ""
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Authorize Steward</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem}"
            ".err{color:#b00}input[type=password]{width:100%;padding:.5rem;margin:.5rem 0}"
            "button{padding:.5rem 1rem;margin-right:.5rem}</style></head><body><main>"
            "<h1>Authorize Steward</h1>"
            f"<p><strong>{esc(client_name)}</strong> (returns to <code>{esc(cb.hostname or '')}</code>) "
            f"is asking to connect to Steward at <code>{esc(self.resource)}</code>.</p>"
            f"<p>It will be allowed to:</p><ul>{scope_items}</ul>"
            "<p>Approving lets this client act with exactly these permissions until you revoke it; "
            "access renews automatically (refresh tokens rotate, 30 days max between uses). "
            "It cannot run shell commands, write files, or type into terminals.</p>"
            f"{err}"
            "<form method=\"post\" action=\"/consent\">"
            f"<input type=\"hidden\" name=\"tx\" value=\"{esc(tx_id, quote=True)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{esc(csrf, quote=True)}\">"
            "<label>Owner passphrase<input type=\"password\" name=\"passphrase\" maxlength=\"4096\" "
            "autocomplete=\"current-password\" autofocus></label>"
            "<button type=\"submit\" name=\"decision\" value=\"approve\">Approve</button>"
            "<button type=\"submit\" name=\"decision\" value=\"deny\">Deny</button>"
            "</form></main></body></html>"
        )
        return status, self._page_headers(callback_origin), body.encode("utf-8")

    # -- /consent ------------------------------------------------------------------------------

    def consent(self, headers: dict, body: bytes) -> Response:
        form = _form(headers, body)
        if form is None or not {"tx", "csrf"} <= set(form) or not set(form) <= {"tx", "csrf", "passphrase", "decision"}:
            return self._error_page(400, "Invalid consent request.")
        tx_id, csrf = form["tx"], form["csrf"]
        decision = form.get("decision", "approve")
        passphrase = form.get("passphrase", "")
        if not _TX_ID.fullmatch(tx_id) or not 1 <= len(csrf) <= 256 or decision not in ("approve", "deny"):
            return self._error_page(400, "Invalid consent request.")

        origin = headers.get("origin", "")
        if not hmac.compare_digest(origin.encode("utf-8", "replace"), self.browser_origin.encode()):
            return self._error_page(403, "Consent refused: cross-origin request.")
        cookie = _cookies(headers).get(_COOKIE_PREFIX + tx_id)
        if cookie is None or not hmac.compare_digest(cookie.encode("utf-8", "replace"), csrf.encode("utf-8", "replace")):
            return self._error_page(403, "Consent refused: missing or mismatched consent cookie.")

        tx = self.store.get_transaction(tx_id)
        now = self.store.now()
        if tx is None or tx["consumed_at"] is not None or now >= tx["expires_at"]:
            return self._error_page(400, "This authorization request has expired. Start linking again.")
        if not hmac.compare_digest(digest(csrf), tx["csrf_digest"]):
            return self._error_page(403, "Consent refused: missing or mismatched consent cookie.")

        client = self.store.get_client(tx["client_id"])
        if client is None:
            return self._error_page(400, "Unknown client.")

        if decision == "deny":
            if not self.store.consume_transaction(tx_id):
                return self._error_page(400, "This authorization request has expired. Start linking again.")
            status, hdrs, _ = self._redirect_error(tx["redirect_uri"], "access_denied", tx["state"])
            return status, hdrs + [self._clear_cookie(tx_id)], b""

        if not verify_passphrase(self.passphrase_record, passphrase):
            self.store.record_failed_attempt(tx_id, MAX_PASSPHRASE_ATTEMPTS)
            return self._consent_page(
                403, client_name=client["client_name"], redirect_uri=tx["redirect_uri"],
                scopes=tx["scopes"].split(), tx_id=tx_id, csrf=csrf,
                error="Wrong passphrase. Nothing was authorized.",
            )

        if not self.store.consume_transaction(tx_id):
            return self._error_page(400, "This authorization request has expired. Start linking again.")
        code = "stw_ac_" + secrets.token_urlsafe(32)
        self.store.create_code(
            code_digest=digest(code),
            tx_id=tx_id,
            client_id=tx["client_id"],
            redirect_uri=tx["redirect_uri"],
            redirect_explicit=bool(tx["redirect_explicit"]),
            code_challenge=tx["code_challenge"],
            scopes=tx["scopes"].split(),
            resource=tx["resource"],
            family_id=secrets.token_hex(16),
            ttl=CODE_TTL,
        )
        values = {"code": code}
        if tx["state"] is not None:
            values["state"] = tx["state"]
        return self._redirect(self._callback(tx["redirect_uri"], values), [self._clear_cookie(tx_id)])

    @staticmethod
    def _clear_cookie(tx_id: str) -> tuple[str, str]:
        return ("Set-Cookie", f"{_COOKIE_PREFIX}{tx_id}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")

    # -- /token ----------------------------------------------------------------------------------

    @staticmethod
    def _basic_client_id(headers: dict) -> str | None:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return None
        try:
            raw = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        return unquote(raw.partition(":")[0]) or None

    def _mint(self) -> tuple[str, str, str, str]:
        access = "stw_at_" + secrets.token_urlsafe(32)
        refresh = "stw_rt_" + secrets.token_urlsafe(32)
        return access, "at_" + secrets.token_hex(8), refresh, "rt_" + secrets.token_hex(8)

    @staticmethod
    def _token_response(access: str, refresh: str, scopes: Iterable[str]) -> Response:
        return _json(200, {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": " ".join(scopes),
        })

    def token(self, headers: dict, body: bytes) -> Response:
        form = _form(headers, body)
        if form is None:
            return _oauth_error(400, "invalid_request", "form-encoded body required")
        client_id = form.get("client_id")
        basic = self._basic_client_id(headers)
        if client_id and basic and client_id != basic:
            return _oauth_error(400, "invalid_request", "conflicting client_id")
        client_id = client_id or basic
        client = self.store.get_client(client_id) if client_id else None
        if client is None:
            return _oauth_error(401, "invalid_client", "unknown client")
        if "resource" in form and form["resource"] != self.resource:
            return _oauth_error(400, "invalid_target", "resource mismatch")

        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return self._token_from_code(client_id, form)
        if grant_type == "refresh_token":
            return self._token_from_refresh(client_id, form)
        return _oauth_error(400, "unsupported_grant_type")

    def _token_from_code(self, client_id: str, form: dict) -> Response:
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        if not code or len(code) > 512:
            return _oauth_error(400, "invalid_grant", "invalid authorization code")
        code_digest = digest(code)
        row = self.store.get_code(code_digest)
        if row is None or row["client_id"] != client_id:
            return _oauth_error(400, "invalid_grant", "invalid authorization code")
        if row["used_at"] is None:
            if self.store.now() >= row["expires_at"]:
                return _oauth_error(400, "invalid_grant", "invalid authorization code")
            if row["redirect_explicit"] or "redirect_uri" in form:
                if form.get("redirect_uri") != row["redirect_uri"]:
                    return _oauth_error(400, "invalid_grant", "redirect_uri mismatch")
            if not _PKCE_VERIFIER.fullmatch(verifier) or not hmac.compare_digest(
                _pkce_s256(verifier), row["code_challenge"]
            ):
                return _oauth_error(400, "invalid_grant", "PKCE verification failed")
        access, access_id, refresh, refresh_id = self._mint()
        # A used code falls through to the store, which revokes its family.
        outcome = self.store.exchange_code(
            code_digest,
            access_digest=digest(access),
            access_id=access_id,
            refresh_digest=digest(refresh),
            refresh_id=refresh_id,
            access_ttl=ACCESS_TTL,
            refresh_ttl=REFRESH_TTL,
        )
        if outcome.status != "ok":
            return _oauth_error(400, "invalid_grant", "invalid authorization code")
        return self._token_response(access, refresh, outcome.scopes)

    def _token_from_refresh(self, client_id: str, form: dict) -> Response:
        presented = form.get("refresh_token", "")
        if not presented or len(presented) > 512:
            return _oauth_error(400, "invalid_grant", "invalid refresh token")
        scopes = None
        if "scope" in form:
            requested = form["scope"].split()
            row = self.store.get_refresh(digest(presented))
            original = set(row["scopes"].split()) if row else set()
            if not requested or not set(requested) <= original:
                return _oauth_error(400, "invalid_scope", "scope exceeds the original grant")
            scopes = _canonical_scopes(requested)
        access, access_id, refresh, refresh_id = self._mint()
        outcome = self.store.rotate_refresh(
            digest(presented),
            client_id=client_id,
            access_digest=digest(access),
            access_id=access_id,
            refresh_digest=digest(refresh),
            refresh_id=refresh_id,
            access_ttl=ACCESS_TTL,
            refresh_ttl=REFRESH_TTL,
            scopes=scopes,
        )
        if outcome.status != "ok":
            return _oauth_error(400, "invalid_grant", "invalid refresh token")
        return self._token_response(access, refresh, outcome.scopes)

    # -- /revoke (RFC 7009) -------------------------------------------------------------------------

    def revoke(self, headers: dict, body: bytes) -> Response:
        form = _form(headers, body)
        if form is None or not form.get("token"):
            return _oauth_error(400, "invalid_request", "token is required")
        token = form["token"]
        if len(token) <= 512:
            self.store.revoke_by_token(digest(token), form.get("client_id") or self._basic_client_id(headers))
        # RFC 7009 §2.2: 200 whether or not the token was known.
        return _json(200, {})

    # -- resource-server side -------------------------------------------------------------------------

    def validate(self, token: str, resource: str | None = None) -> AccessGrant:
        """Validate an OAuth access token for `resource` (default: this server's
        exact `<base>/mcp`). Raises `InvalidToken`; returns the granted scopes."""
        if resource is None:
            resource = self.resource
        if resource != self.resource or not token or len(token) > 512:
            raise InvalidToken("invalid_token")
        row = self.store.get_access(digest(token))
        if (
            row is None
            or row["revoked"]
            or self.store.now() >= row["expires_at"]
            or row["resource"] != self.resource
        ):
            raise InvalidToken("invalid_token")
        return AccessGrant(token_id=row["token_id"], client_id=row["client_id"], scopes=tuple(row["scopes"].split()))
