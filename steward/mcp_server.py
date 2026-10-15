"""stdlib MCP server: JSON-RPC 2.0 over HTTP, localhost-only (MCP-01, SP-26).

`MCPServer` holds the tool registry (a list of `ToolSpec`, wired up for real
by `steward.cli`'s `mcp serve` from `steward.api.StewardAPI`) and answers
JSON-RPC 2.0 requests (`initialize`, `notifications/initialized`,
`tools/list`, `tools/call`, `ping`) with no transport of its own —
`make_http_server` wraps it in a stdlib `http.server.ThreadingHTTPServer`
bound to `127.0.0.1` (HARD SAFETY: refuses any other host unless the caller
explicitly passes `allow_host=True` — nothing here starts a tunnel or
otherwise exposes the socket).

Every `/mcp` request is bearer-authenticated through `steward.mcp_auth`
before any method runs; `GET /healthz` is the one unauthenticated route and
only ever returns `{"ok": true}`. A `tools/call` additionally checks the
tool's own `required_scope` against the token before the handler runs, so an
insufficient-scope call never reaches `steward.api` at all — no side
effects, matching the CONFLICT-style "call rejected, nothing changed"
contract the rest of Steward uses (SP-27, SP-29).

`ThreadingHTTPServer` runs one Python thread per HTTP request, all sharing
the one `StewardAPI`/`Store` a server was built with. `steward/store.py`'s
sqlite3 connection is safe to call from multiple threads (serialized
threadsafety), but a single tool call is not one sqlite call — it's a
read-modify-write sequence at the Python level (e.g. `Steering.record`
reads the kv-backed instruction list, appends, then saves; bumps a revision
counter as a separate step). Two `tools/call`s running that sequence at the
same time could interleave and silently lose an update (classic
read-modify-write race) even though each individual sqlite statement is
safe. `MCPServer` therefore serializes every tool *handler* invocation with
a single lock — one call runs the read-modify-write sequence start to
finish before the next begins — trading a small amount of concurrency for
the same single-writer guarantee the rest of Steward already relies on
(SP-1, SP-14).

Public serving (LIVE-02, LIVE-03): `make_http_server(..., oauth=OAuthServer)`
(or `serve(..., public_base_url=...)`) adds the `steward.oauth` authorization
server routes and hardens the transport for exposure through a tunnel. The
socket still binds 127.0.0.1 only. Every request passes, in order: Host check
(public host or `127.0.0.1:<port>`, else 421) -> exact path allowlist (else
404) -> 64KB body cap incl. chunked bodies (else 413) -> per-client-IP token
bucket on /register /authorize /consent /token /revoke (else 429; the client
IP is `CF-Connecting-IP` only when `trust_forwarded`) -> handler. `/mcp`
accepts static bearer tokens (`steward.mcp_auth`) or OAuth access tokens
validated against the exact `<base>/mcp` resource; a 401 carries
`WWW-Authenticate: Bearer resource_metadata="<base>/.well-known/oauth-protected-resource/mcp"`.
One structured JSON access-log line per request (method, path without query,
status, duration, client IP, and only a short token-id prefix -- never token
material, cookies, bodies or query strings).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from steward import mcp_auth
from steward import owner
from steward.oauth import InvalidToken, OAuthServer
from steward.api import StewardAPI
from steward import approvals as approvals_module
from steward import custom_watchers as custom_watchers_module
from steward import evidence as evidence_module
from steward import incorporate as incorporate_module
from steward import questions as questions_module
from steward import session as session_module
from steward import supervisors as supervisors_module
from steward import tokens as tokens_module
from steward import transcript as transcript_module
from steward import watchers as watchers_module
from steward.diagnosis import redact
from steward.models import JobState
from steward.roles import role_key, role_label

PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
SERVER_NAME = "steward"
SERVER_VERSION = "0.1.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001
INSUFFICIENT_SCOPE = -32003


class ToolError(Exception):
    """Raised by dispatch (or a tool handler) to produce a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    annotations: dict
    required_scope: str
    handler: Callable[[dict, mcp_auth.TokenInfo], dict]


class MCPServer:
    """Protocol + auth logic, independent of any socket (tested without one)."""

    def __init__(
        self,
        tools: list[ToolSpec],
        *,
        home: Path | str | None = None,
        oauth: OAuthServer | None = None,
        anonymous_scopes: tuple[str, ...] | None = None,
    ):
        self.tools: dict[str, ToolSpec] = {t.name: t for t in tools}
        # OpenAI Secure MCP Tunnel mode: the only client is OpenAI's outbound
        # tunnel-client on this host, so requests carry no credential and get
        # exactly these scopes. Never combined with public serving (see serve()).
        self.anonymous_scopes = tuple(anonymous_scopes) if anonymous_scopes is not None else None
        self.home = home
        # When set, OAuth access tokens (validated against the exact public
        # `<base>/mcp` resource) are accepted alongside static bearer tokens.
        self.oauth = oauth
        # Serializes tool-handler execution across ThreadingHTTPServer's
        # per-request threads (see module docstring): one handler's
        # read-modify-write sequence against the store completes before the
        # next begins, so concurrent tool calls can never interleave and
        # lose an update.
        self._dispatch_lock = threading.Lock()

    # -- transport-agnostic entry point --------------------------------------

    def authenticate(self, auth_header: str | None) -> mcp_auth.TokenInfo:
        """Resolve a bearer header to a `TokenInfo` or raise `mcp_auth.Unauthorized`.

        OAuth access tokens are tried first (when OAuth is configured), then
        the static `mcp_auth` token store; both are digest lookups.
        """
        if self.anonymous_scopes is not None:
            return mcp_auth.TokenInfo(id="openai-tunnel", scopes=self.anonymous_scopes)
        if self.oauth is not None and auth_header and auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer "):].strip()
            try:
                grant = self.oauth.validate(token)
            except InvalidToken:
                pass
            else:
                return mcp_auth.TokenInfo(id=grant.token_id, scopes=grant.scopes)
        return mcp_auth.authorize(auth_header, None, home=self.home)

    def handle_request(self, raw: bytes, auth_header: str | None) -> tuple[int, bytes]:
        """Return `(http_status, response_body_bytes)` for one HTTP POST /mcp.

        Authentication happens before the body is parsed, so an
        unauthenticated caller learns nothing but "401".
        """
        try:
            token_info = self.authenticate(auth_header)
        except mcp_auth.Unauthorized as exc:
            return 401, self._dump(self._error(None, UNAUTHORIZED, str(exc)))
        return self.handle_authenticated(raw, token_info)

    def handle_authenticated(self, raw: bytes, token_info: mcp_auth.TokenInfo) -> tuple[int, bytes]:
        try:
            payload = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 200, self._dump(self._error(None, PARSE_ERROR, "parse error"))

        if payload is None:
            return 200, self._dump(self._error(None, INVALID_REQUEST, "empty request body"))

        if isinstance(payload, list):
            if not payload:
                return 200, self._dump(self._error(None, INVALID_REQUEST, "empty batch"))
            responses = [r for r in (self._dispatch(item, token_info) for item in payload) if r is not None]
            if not responses:
                return 202, b""
            return 200, self._dump(responses)

        response = self._dispatch(payload, token_info)
        if response is None:
            return 202, b""
        return 200, self._dump(response)

    # -- per-call dispatch ----------------------------------------------------

    def _dispatch(self, item: Any, token_info: mcp_auth.TokenInfo) -> dict | None:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or "method" not in item:
            request_id = item.get("id") if isinstance(item, dict) else None
            return self._error(request_id, INVALID_REQUEST, "invalid JSON-RPC request")

        request_id = item.get("id")
        is_notification = "id" not in item
        method = item["method"]
        params = item.get("params") or {}

        if method == "notifications/initialized":
            return None

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = self._tools_call(params, token_info)
            else:
                raise ToolError(METHOD_NOT_FOUND, f"method not found: {method}")
        except ToolError as exc:
            if is_notification:
                return None
            return self._error(request_id, exc.code, exc.message, exc.data)
        except mcp_auth.InsufficientScope as exc:
            if is_notification:
                return None
            return self._error(
                request_id,
                INSUFFICIENT_SCOPE,
                "insufficient_scope",
                {"required_scope": exc.required_scope},
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods ----------------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in _SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}},
        }

    def _tools_list(self) -> dict:
        return {"tools": [self._tool_json(t) for t in self.tools.values()]}

    @staticmethod
    def _tool_json(tool: ToolSpec) -> dict:
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "annotations": tool.annotations,
        }

    def _tools_call(self, params: dict, token_info: mcp_auth.TokenInfo) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(INVALID_PARAMS, f"unknown tool: {name!r}")
        if tool.required_scope not in token_info.scopes:
            raise mcp_auth.InsufficientScope(tool.required_scope)

        try:
            with self._dispatch_lock:
                structured = tool.handler(arguments, token_info)
            is_error = False
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool failure is a result, not a transport fault
            structured = {"error": str(exc)}
            is_error = True

        return {
            "content": [{"type": "text", "text": json.dumps(structured)}],
            "structuredContent": structured,
            "isError": is_error,
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
        error: dict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _dump(obj: Any) -> bytes:
        return json.dumps(obj).encode("utf-8")


# -- HTTP transport (127.0.0.1 only) -----------------------------------------

MAX_BODY_BYTES = 64 * 1024
# Requests per minute per (client IP, path). Linking needs ~1 each; ChatGPT
# refreshes about hourly.
RATE_LIMITS = {
    "/register": 10,
    "/consent": 10,
    "/token": 10,
    "/revoke": 10,
    "/authorize": 30,
}
_ALL_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")


class RateLimiter:
    """In-memory token bucket per key: `limit` tokens, refilled over 60s."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 4096):
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * limit / 60.0)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)  # re-insert: dict order == recency
            while len(self._buckets) > self._max_keys:
                self._buckets.pop(next(iter(self._buckets)))
            return allowed


class _BodyTooLarge(Exception):
    pass


class _BadBody(Exception):
    pass


def _stderr_access_log(line: dict) -> None:
    print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)


def _token_id_prefix(token_id: str) -> str:
    # oauth ids are "at_<16 hex>", static ids are 16 hex: keep ≤ 8 id chars.
    return token_id[:11] if token_id.startswith("at_") else token_id[:8]


def make_http_server(
    mcp_server: MCPServer,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    allow_host: bool = False,
    oauth: OAuthServer | None = None,
    trust_forwarded: bool = False,
    access_log: Callable[[dict], None] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> ThreadingHTTPServer:
    """Bind a `ThreadingHTTPServer` serving `mcp_server`.

    HARD SAFETY: refuses to bind any host other than `127.0.0.1` unless the
    caller explicitly passes `allow_host=True` — this function never starts
    a tunnel and never registers anything with a public service; it only
    opens a local socket. Passing `oauth` enables the OAuth routes and makes
    the public host (plus `127.0.0.1:<port>`) the only accepted Host values.
    """
    if host != "127.0.0.1" and not allow_host:
        raise ValueError(
            f"refusing to bind host {host!r}: the Steward MCP server binds "
            "127.0.0.1 only unless allow_host=True is passed explicitly "
            "(public exposure goes through a tunnel, see docs/public-connector.md)"
        )
    if oauth is not None:
        mcp_server.oauth = oauth
    handler_cls = _build_handler(
        mcp_server,
        oauth=mcp_server.oauth,
        trust_forwarded=trust_forwarded,
        access_log=access_log or _stderr_access_log,
        limiter=rate_limiter or RateLimiter(),
    )
    return ThreadingHTTPServer((host, port), handler_cls)


def serve(
    mcp_server: MCPServer,
    *,
    port: int = 9520,
    public_base_url: str | None = None,
    trust_forwarded: bool = False,
    passphrase_record: dict | None = None,
    oauth_home: Path | str | None = None,
    redirect_hosts: tuple[str, ...] | None = None,
    browser_base_url: str | None = None,
    grant_scopes: tuple[str, ...] | None = None,
    ready: Callable[[ThreadingHTTPServer], None] | None = None,
    access_log: Callable[[dict], None] | None = None,
) -> None:
    """Run the MCP server in the foreground (blocks until shutdown).

    With `public_base_url` the OAuth authorization server is enabled, backed
    by `$STEWARD_HOME/oauth.db` (or `oauth_home`), and `passphrase_record`
    (the scrypt record from `steward mcp bootstrap`) is required.
    """
    oauth_server = None
    store = None
    if public_base_url and mcp_server.anonymous_scopes is not None:
        raise ValueError("anonymous (tunnel) mode must never be served publicly")
    if public_base_url:
        if not passphrase_record:
            raise ValueError("public serving requires the owner passphrase record (run `steward mcp bootstrap`)")
        from steward.oauth_store import OAuthStore
        from steward.oauth import DEFAULT_REDIRECT_HOSTS

        store = OAuthStore(home=oauth_home if oauth_home is not None else mcp_server.home)
        oauth_server = OAuthServer(
            store,
            base_url=public_base_url,
            passphrase_record=passphrase_record,
            redirect_hosts=redirect_hosts or DEFAULT_REDIRECT_HOSTS,
            browser_base_url=browser_base_url,
            **({"grant_scopes": grant_scopes} if grant_scopes else {}),
        )
    httpd = make_http_server(
        mcp_server, port=port, oauth=oauth_server, trust_forwarded=trust_forwarded, access_log=access_log
    )
    if ready is not None:
        ready(httpd)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if store is not None:
            store.close()


def _build_handler(
    mcp_server: MCPServer,
    *,
    oauth: OAuthServer | None,
    trust_forwarded: bool,
    access_log: Callable[[dict], None],
    limiter: RateLimiter,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "steward-mcp/0.1"
        sys_version = ""
        timeout = 30  # per-socket-op timeout: a stalled client can't pin a thread forever

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            pass  # replaced by the structured access log below

        # every method goes through one router so the allowlist is total
        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._route("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._route("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._route("PATCH")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._route("OPTIONS")

        def do_HEAD(self) -> None:  # noqa: N802
            self._route("HEAD")

        # -- plumbing -------------------------------------------------------

        def _route(self, method: str) -> None:
            started = time.monotonic()
            self._status = 0
            self._tid: str | None = None
            self._err: str | None = None
            self._method = method
            path = self.path.split("?", 1)[0]
            try:
                self._dispatch(method, path)
            except Exception as exc:  # noqa: BLE001 - never leak internals to a public caller
                self._err = type(exc).__name__  # type only: messages may carry request data
                if not self._status:
                    self._respond(500, b'{"error": "internal error"}')
            finally:
                line = {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "method": method,
                    "path": path[:200],
                    "status": self._status,
                    "ms": round((time.monotonic() - started) * 1000, 1),
                    "ip": self._client_ip(),
                }
                if self._tid:
                    line["tid"] = self._tid
                if self._err:
                    line["err"] = self._err
                try:
                    access_log(line)
                except Exception:  # noqa: BLE001 - logging must never break serving
                    pass

        def _client_ip(self) -> str:
            if trust_forwarded:
                forwarded = self.headers.get("CF-Connecting-IP")
                if forwarded:
                    return forwarded.strip()[:64]
            return self.client_address[0]

        def _host_ok(self) -> bool:
            values = self.headers.get_all("Host") or []
            if len(values) != 1:
                return False
            host = values[0].strip().lower()
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}"}
            if oauth is not None:
                allowed.add(oauth.host)
                allowed.add(oauth.browser_host)
            else:
                allowed.add(f"localhost:{port}")
            return host in allowed

        def _read_body(self) -> bytes:
            te = self.headers.get("Transfer-Encoding")
            cl = self.headers.get("Content-Length")
            if te is not None:
                if cl is not None or te.strip().lower() != "chunked":
                    raise _BadBody()
                return self._read_chunked()
            if cl is None:
                return b""
            try:
                length = int(cl)
            except ValueError:
                raise _BadBody() from None
            if length < 0:
                raise _BadBody()
            if length > MAX_BODY_BYTES:
                raise _BodyTooLarge()
            return self.rfile.read(length) if length else b""

        def _read_chunked(self) -> bytes:
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(1024)
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise _BadBody() from None
                if size < 0:
                    raise _BadBody()
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass  # discard trailers
                    return b"".join(chunks)
                total += size
                if total > MAX_BODY_BYTES:
                    raise _BodyTooLarge()
                chunks.append(self.rfile.read(size))
                self.rfile.readline(8)  # CRLF after chunk data

        def _headers_dict(self) -> dict[str, str]:
            out: dict[str, str] = {}
            for name in set(k.lower() for k in self.headers.keys()):
                values = self.headers.get_all(name) or []
                if name == "cookie":
                    out[name] = "; ".join(values)
                elif len(values) == 1:
                    out[name] = values[0]
                else:
                    out[name] = ""  # ambiguous duplicate header: treat as absent
            return out

        def _respond(
            self,
            status: int,
            body: bytes,
            headers: list[tuple[str, str]] | None = None,
        ) -> None:
            self._status = status
            headers = list(headers or [])
            if not any(k.lower() == "content-type" for k, _ in headers) and body:
                headers.append(("Content-Type", "application/json"))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            if status in (400, 413):
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if body and self._method != "HEAD":
                self.wfile.write(body)

        def _json_error(self, status: int, message: str, headers=None) -> None:
            self._respond(status, json.dumps({"error": message}).encode("utf-8"), headers)

        def _challenge(self, auth_header: str | None) -> list[tuple[str, str]]:
            if oauth is None:
                return [("WWW-Authenticate", "Bearer")]
            return [("WWW-Authenticate", oauth.www_authenticate("invalid_token" if auth_header else None))]

        # -- routing --------------------------------------------------------

        def _dispatch(self, method: str, path: str) -> None:
            if not self._host_ok():
                self._json_error(421, "misdirected request")
                return
            if (
                oauth is not None
                and oauth.browser_host != oauth.host
                and (self.headers.get("Host") or "").strip().lower() == oauth.browser_host
                and path not in ("/authorize", "/consent")
            ):
                # The browser-only origin serves the consent flow and nothing else.
                self._json_error(404, "not found")
                return

            if path == "/healthz":
                if method != "GET":
                    self._json_error(405, "method not allowed", [("Allow", "GET")])
                    return
                self._respond(200, json.dumps({"ok": True}).encode("utf-8"))
                return

            if path == "/mcp":
                self._serve_mcp(method)
                return

            if oauth is not None and oauth.handles(path):
                self._serve_oauth(method, path)
                return

            self._json_error(404, "not found")

        def _body_or_error(self) -> bytes | None:
            try:
                return self._read_body()
            except _BodyTooLarge:
                self._json_error(413, "request body too large")
            except _BadBody:
                self._json_error(400, "malformed request body")
            return None

        def _serve_mcp(self, method: str) -> None:
            auth_header = self.headers.get("Authorization")
            if method == "POST":
                raw = self._body_or_error()
                if raw is None:
                    return
            try:
                token_info = mcp_server.authenticate(auth_header)
            except mcp_auth.Unauthorized as exc:
                body = MCPServer._dump(MCPServer._error(None, UNAUTHORIZED, str(exc)))
                self._respond(401, body, self._challenge(auth_header))
                return
            self._tid = _token_id_prefix(token_info.id)
            if method != "POST":
                self._json_error(405, "method not allowed", [("Allow", "POST")])
                return
            status, body = mcp_server.handle_authenticated(raw, token_info)
            self._respond(status, body, [("Cache-Control", "no-store")])

        def _serve_oauth(self, method: str, path: str) -> None:
            body = self._body_or_error()
            if body is None:
                return
            limit = RATE_LIMITS.get(path)
            if limit is not None and method == ("GET" if path == "/authorize" else "POST"):
                if not limiter.allow(f"{self._client_ip()}|{path}", limit):
                    self._json_error(429, "too many requests", [("Retry-After", "60")])
                    return
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            status, headers, resp = oauth.handle(method, path, query, self._headers_dict(), body)
            self._respond(status, resp, headers)

    return Handler


# -- tool surface over steward.api (MCP-04, SP-26) ---------------------------

TOOL_NAMES = (
    "list_jobs",
    "get_job",
    "get_current_step",
    "get_recent_events",
    "get_verification",
    "get_diff_summary",
    "explain_blocker",
    "explain_job",
    "steer_job",
    "pause_job",
    "resume_job",
    "cancel_job",
    "approve_action",
    "get_supervisors",
    "get_watchers_feed",
    "get_tokens",
    "get_worker_activity",
    "get_decision",
    "preview_incorporate",
    "accept_job",
    "reject_job",
    "send_back_job",
    "release_job",
    "incorporate_job",
    "toggle_supervisor",
    "add_watcher",
    "remove_watcher",
    "create_job",
    "launch_job",
    "play_job",
    "get_result",
    "get_questions",
    "answer_questions",
)

_ALLOWED_STATES = {s.value for s in JobState}
# Same "still worth a glance" set as `steward tokens` / the phone screen.
_ACTIVE_STATES = frozenset(JobState) - {JobState.STOPPED, JobState.FAILED, JobState.RELEASED}
_Q_QUESTION_CAP = 300
_Q_DETAIL_CAP = 200
_Q_MAX_ANSWERS = 15
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _require(arguments: dict, *names: str) -> None:
    missing = [name for name in names if name not in arguments]
    if missing:
        raise ToolError(INVALID_PARAMS, f"missing required argument(s): {', '.join(missing)}")


def build_tools(api: StewardAPI) -> list[ToolSpec]:
    """The exact SP-26 tool set (MCP-04): 1:1 onto `steward.api.StewardAPI` —
    this is the only place `steward.mcp_server` calls into it; nothing here
    touches `store`/`runner`/worker/herdr directly, and no tool can run a
    shell command, write a file, or send keys.

    The write tools (`steer_job`, `pause_job`, `resume_job`, `cancel_job`,
    `approve_action`) always pass `origin="chatgpt"`; whether the call
    actually relays into the live Herdr control pane (OPX-03) or executes
    directly against the store is `api`'s own concern (`StewardAPI(...,
    relay=...)`) — `steward.cli`'s MCP-serving commands are the only place
    that constructs one with a relay wired up. A tool-handler exception
    (including "no control pane") is caught by `MCPServer._tools_call`'s
    generic `isError` path, never a bare JSON-RPC transport error."""

    def list_jobs(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        states = arguments.get("states")
        state_set = None
        if states is not None:
            unknown = [s for s in states if s not in _ALLOWED_STATES]
            if unknown:
                raise ToolError(INVALID_PARAMS, f"unknown state(s): {unknown}")
            state_set = {JobState(s) for s in states}
        return {"jobs": api.list_jobs(states=state_set)}

    def get_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return {"job": api.get_job(arguments["job_id"])}

    def get_current_step(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return {"step": api.get_current_step(arguments["job_id"])}

    def get_recent_events(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        limit = arguments.get("limit", 20)
        return {"events": api.get_recent_events(arguments["job_id"], limit=limit)}

    def get_verification(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return {"verification": api.get_verification(arguments["job_id"])}

    def get_diff_summary(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return {"diff": api.get_diff_summary(arguments["job_id"])}

    def explain_blocker(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return {"blocker": api.explain_blocker(arguments["job_id"])}

    def explain_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        from steward import explain as explain_module

        try:
            d = explain_module.data(api.store, arguments["job_id"])
        except KeyError:
            return {"explanation": None, "text": None, "detail": f"unknown job {arguments['job_id']}"}
        return {"explanation": d, "text": explain_module.render(d)}

    def steer_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "expected_revision", "instruction", "human_instruction")
        return api.steer(
            arguments["job_id"],
            arguments["instruction"],
            origin="chatgpt",
            expected_revision=arguments["expected_revision"],
            human_instruction=arguments["human_instruction"],
            interpreted_instruction=arguments["instruction"],
            verb=arguments.get("verb", "nudge"),
            request_id=arguments.get("request_id"),
            urgent=bool(arguments.get("urgent", False)),
        )

    def pause_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return api.pause(arguments["job_id"], origin="chatgpt", request_id=arguments.get("request_id"))

    def resume_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return api.resume(arguments["job_id"], origin="chatgpt", request_id=arguments.get("request_id"))

    def cancel_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return api.cancel(arguments["job_id"], origin="chatgpt", request_id=arguments.get("request_id"))

    def approve_action(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "approval_id")
        return api.approve(
            arguments["job_id"],
            arguments["approval_id"],
            origin="chatgpt",
            decision=arguments.get("decision", "approve"),
            request_id=arguments.get("request_id"),
        )


    # -- supervisors / watchers / tokens / decisions / closeout --------------

    def _known_job(job_id: str):
        job = api.store.get_job(job_id)
        if job is None:
            raise ToolError(INVALID_PARAMS, f"unknown job: {job_id}")
        return job

    def _write(op: str, args: dict, arguments: dict) -> dict:
        return api.relay_only(op, args, origin="chatgpt", request_id=arguments.get("request_id"))

    def get_supervisors(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        _known_job(job_id)
        sources = tokens_module.job_tokens(api.store, job_id)["sources"]
        rows = []
        for r in supervisors_module.supervisor_rows(api.store, job_id):
            src = r.get("source") or r["role"]
            tok = sources.get(src)
            rows.append({
                "label": r.get("label", r["role"]),
                "role": r["role"],
                "model": r["model"],
                "switch": r.get("switch"),
                "status": r["status"],
                "display": r["display"].replace("{SPIN}", "*"),
                "next_switch_state": r.get("next"),
                "tokens": ({"total": tok["all"], "last_hour": tok["hour"], "output": tok["out"],
                            "effective": tok["effective"], "usd": round(tok["usd"], 4)} if tok else None),
            })
        return {"job_id": job_id, "supervisors": rows}

    def get_watchers_feed(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        _known_job(job_id)
        last = int(arguments.get("last", 20))
        items = watchers_module.collect(api.store.events(job_id))
        items = items[-last:] if last > 0 else []
        lines = [watchers_module.render_line(ev, role, text, width=400, color=False) for ev, role, text in items]
        return {"job_id": job_id, "lines": lines}

    def get_worker_activity(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        _known_job(job_id)
        act = transcript_module.worker_activity(api.store, job_id, last=int(arguments.get("last", 10)),
                                                since=arguments.get("since"))
        return {**act, "text": transcript_module.render_text(act)}

    def _token_block(b: dict) -> dict:
        return {
            "total": b["all"], "cached": b["cache_read"], "fresh": b["input"] + b["cache_creation"],
            "output": b["output"], "effective": b["effective"], "usd": round(b["usd"], 4),
        }

    def get_tokens(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        job_id = arguments.get("job_id")
        if job_id:
            _known_job(job_id)
            ids = [job_id]
        else:
            ids = [j["job_id"] for j in api.list_jobs(states=_ACTIVE_STATES)]
        data = tokens_module.all_tokens(api.store, ids)
        jobs = []
        for jid, r in data["jobs"].items():
            by_source = {
                role_label(k): {"total": v["all"], "last_hour": v["hour"], "output": v["out"],
                                "effective": v["effective"], "usd": round(v["usd"], 4)}
                for k, v in r["sources"].items() if v["all"]
            }
            jobs.append({"job_id": jid, "total": _token_block(r["total"]),
                         "last_hour": _token_block(r["last_hour"]), "by_source": by_source})
        g = data["grand"]
        return {"jobs": jobs, "grand_total": _token_block(g["total"]), "grand_last_hour": _token_block(g["last_hour"])}

    def _decision_for(job_id: str) -> dict | None:
        dec = evidence_module.decision(api.store, job_id)
        if dec is None:
            return None
        rec = dec.get("approval")
        actions = []
        for _label, action in evidence_module.buttons_for(dec):
            if action == "accept":
                actions.append({"action": "accept", "tool": "accept_job", "arguments": {"job_id": job_id}})
            elif action == "stop":  # acceptance stop == the reject_job tool (deny acceptance -> STOPPED)
                actions.append({"action": "stop", "tool": "reject_job", "arguments": {"job_id": job_id}})
            elif action in ("approve", "deny") and rec:
                actions.append({"action": action, "tool": "approve_action",
                                "arguments": {"job_id": job_id, "approval_id": rec["id"], "decision": action}})
            elif action == "why":
                actions.append({"action": "why", "tool": "explain_blocker", "arguments": {"job_id": job_id}})
        return {
            "job_id": job_id,
            "goal": api.store.get_job(job_id).goal,
            "kind": dec["kind"],
            "approval_id": rec["id"] if rec else None,
            "question": rec["question"] if rec else None,
            "evidence": evidence_module.panel(api.store, job_id, width=400, dec=dec),
            "actions": actions,
        }

    def get_decision(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        job_id = arguments.get("job_id")
        if job_id:
            _known_job(job_id)
            ids = [job_id]
        else:
            ids = [j["job_id"] for j in api.list_jobs(states=_ACTIVE_STATES)]
        decisions = [d for d in (_decision_for(i) for i in ids) if d is not None]
        return {f"waiting_on_{owner.actor_key()}": len(decisions), "decisions": decisions}

    def preview_incorporate(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        job = _known_job(job_id)
        if job.state not in (JobState.ACCEPTED, JobState.RELEASED):
            raise ToolError(
                INVALID_PARAMS,
                f"job {job_id[:8]} is {job.state.value}; incorporate needs ACCEPTED (or RELEASED)",
            )
        pf = incorporate_module.preflight(api.store, job_id)
        return {
            "job_id": job_id, "dry_run": True, "text": "\n".join(pf.lines(width=0)),
            "source_branch": pf.source_branch, "job_branch": pf.branch, "commit_count": pf.commit_count,
            "commits": pf.commits, "diffstat": pf.diffstat, "files": pf.files, "dirty_overlap": pf.overlap,
            "predicted_conflicts": pf.conflicts, "already_contained": pf.contained,
            "blocked": pf.block or None, "error": pf.error or None,
        }

    def accept_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return _write("accept", {"job_id": arguments["job_id"]}, arguments)

    def release_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return _write("release", {"job_id": arguments["job_id"]}, arguments)

    def reject_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        _known_job(job_id)
        pending = [a for a in approvals_module.list_approvals(api.store, job_id, status="pending")
                   if evidence_module._is_acceptance(a)]
        if not pending:
            raise ToolError(INVALID_PARAMS, f"job {job_id[:8]} has no pending acceptance to reject")
        return api.approve(job_id, pending[0]["id"], origin="chatgpt", decision="deny",
                           request_id=arguments.get("request_id"))

    def send_back_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "instructions")
        job_id = arguments["job_id"]
        job = _known_job(job_id)
        if job.state not in (JobState.NEEDS_HUMAN, JobState.WORKER_FINISHED):  # sendback.send_back decides the rest
            raise ToolError(INVALID_PARAMS, f"job {job_id[:8]} is {job.state.value}; send_back_job needs NEEDS_HUMAN "
                                            "or WORKER_FINISHED")
        return _write("send_back", {"job_id": job_id, "instructions": str(arguments["instructions"]),
                                    "goal_append": bool(arguments.get("goal_append", False))}, arguments)

    def incorporate_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        return _write("incorporate", {"job_id": arguments["job_id"], "release": bool(arguments.get("release", False))},
                      arguments)

    def toggle_supervisor(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "role")
        job_id = arguments["job_id"]
        role = role_key(str(arguments["role"]).strip().lower())
        state = arguments.get("state") or "toggle"
        if role in supervisors_module.ROLE_STATES:
            return _write("supervisor", {"job_id": job_id, "role": role, "state": state}, arguments)
        if state not in ("on", "off", "toggle"):
            raise ToolError(INVALID_PARAMS, f"a custom watcher accepts on|off|toggle, not {state}")
        return _write("watcher", {"action": "toggle", "job_id": job_id, "name": role, "state": state}, arguments)

    def add_watcher(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "name", "ask")
        return _write("watcher", {
            "action": "add", "job_id": arguments["job_id"], "name": arguments["name"], "ask": arguments["ask"],
            "model": arguments.get("model", "haiku"), "every": arguments.get("every", "10m"),
            "may": arguments.get("may") or [],
        }, arguments)

    def remove_watcher(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id", "name")
        return _write("watcher", {"action": "rm", "job_id": arguments["job_id"], "name": arguments["name"]}, arguments)

    def create_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "goal", "repo")
        args: dict = {"goal": arguments["goal"], "repo": arguments["repo"], "no_baseline": False}
        if arguments.get("finish_line"):
            args["goal"] = f"{arguments['goal']}. Done when: {arguments['finish_line']}"
        specs = []
        for w in arguments.get("watchers") or []:
            try:
                spec = custom_watchers_module.make_spec(
                    w.get("name", ""), w.get("ask", ""), model=w.get("model"), every=w.get("every"), may=w.get("may"))
            except custom_watchers_module.WatcherError as exc:
                raise ToolError(INVALID_PARAMS, f"bad watcher: {exc}") from exc
            specs.append({"name": spec["name"], "ask": spec["ask"], "model": spec["model"],
                          "every_s": spec["every_s"], "may": spec["may"]})
        if specs:
            args["watchers"] = specs
        if arguments.get("follow_up"):
            args["follow_up"] = str(arguments["follow_up"])
        if arguments.get("research"):
            args["research"] = True
        keys = arguments.get("grant_keys") or []
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ToolError(INVALID_PARAMS, "grant_keys must be a list of key names")
        if keys:
            args["grant_keys"] = keys
        if arguments.get("research_cap") is not None:
            args["research_cap"] = float(arguments["research_cap"])
        receipt = _write("new", args, arguments)
        receipt["next"] = f"Read contract_text back to {owner.name()}; only call launch_job after {owner.subj()} explicitly {owner.verb('says')} go."
        return receipt

    def get_result(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        _known_job(arguments["job_id"])
        return session_module.build_result(api.store, arguments["job_id"])

    def launch_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        model = str(arguments.get("model", "sonnet"))
        if not _MODEL_RE.match(model):
            raise ToolError(INVALID_PARAMS, f"bad model {model!r}")
        return _write("launch", {
            "job_id": arguments["job_id"], "worker": "fabric", "kind": "claude",
            "agent_args": ["--model", model], "workspace": None, "interval": 15.0, "sock": None,
            "keep_pane": False,
        }, arguments)

    def play_job(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        """`steward play`: launch a staged job with its recorded launch args (else the default worker model)."""
        _require(arguments, "job_id")
        job = _known_job(arguments["job_id"])
        state = getattr(job, "state", None)
        state = getattr(state, "value", state)
        if state == "CREATED":
            raise ToolError(INVALID_PARAMS, "baseline still running; play once the job is CONTRACTED")
        if state != "CONTRACTED":
            return {"accepted": True, "detail": f"already {state}; nothing to launch"}
        recorded = dict(api.store.kv_get(arguments["job_id"], "launch_args") or {})
        return _write("launch", {
            "worker": "fabric", "kind": "claude", "agent_args": ["--model", "sonnet"], "workspace": None,
            "interval": 15.0, "sock": None, "keep_pane": False,
            **{k: v for k, v in recorded.items() if k in ("worker", "kind", "agent_args", "interval", "keep_pane")},
            "job_id": arguments["job_id"],
        }, arguments)

    # -- the question queue (Phase 17-06, G3/G4/G13) -----------------------------------------------------------

    def _q_text(text, cap: int) -> str:
        return redact(re.sub(r"\s+", " ", str(text or "")).strip())[:cap].rstrip()

    def _mcp_card(card: dict) -> dict:
        """One card for a remote model: recommended option first (questions.card already orders it), text capped."""
        out = {
            "qid": card["qid"], "anchor": card["anchor"], "title": _q_text(card["title"], 120),
            "question": _q_text(card["question"], _Q_QUESTION_CAP), "status": card["status"],
            "starred": card["starred"], "recommended": card["recommended"],
            "recommended_text": _q_text(card["recommended_text"], 120),
            "blocking": card["blocking"], "blocking_reason": _q_text(card["blocking_reason"], 200),
            "rationale": _q_text(card["rationale"], _Q_DETAIL_CAP),
            "options": [{"key": o["key"], "label": _q_text(o["label"], 120), "detail": _q_text(o["detail"], _Q_DETAIL_CAP),
                         "recommended": o["recommended"]} for o in card["options"]],
        }
        if card.get("stale_from"):
            out["stale"] = True
        ans = card.get("answer")
        if ans:
            out["answer"] = {"option": ans.get("option", ""), "other": _q_text(ans.get("other"), 300),
                             "note": _q_text(ans.get("note"), 200), "origin": ans.get("origin", "")}
        return out

    def get_questions(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        include = str(arguments.get("include") or "waiting")
        if include not in ("waiting", "all"):
            raise ToolError(INVALID_PARAMS, 'include must be "waiting" or "all"')
        round_no = arguments.get("round")
        if round_no is not None and (isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1):
            raise ToolError(INVALID_PARAMS, "round must be an integer >= 1")
        limit = arguments.get("limit", 15)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ToolError(INVALID_PARAMS, "limit must be an integer from 1 to 50")
        job_id = arguments.get("job_id")
        if job_id:
            _known_job(job_id)
            ids = [job_id]
        else:
            ids = [j["job_id"] for j in api.list_jobs(states=_ACTIVE_STATES)]
        jobs = []
        waiting = 0
        for jid in ids:
            records = questions_module.load(api.store, jid)
            summary = questions_module.summary(records)
            if not job_id and not (summary["waiting"] if include == "waiting" else records):
                continue
            view = questions_module.view(api.store, jid, include_all=include == "all", round_no=round_no)
            waiting += summary["waiting"]
            entry = {"job_id": jid, "goal": _q_text(api.store.get_job(jid).goal, 200), "summary": summary,
                     "questions": [_mcp_card(c) for c in view["questions"][:limit]]}
            if round_no is not None:
                entry["round"], entry["rounds"] = view["round"], view["rounds"]
            jobs.append(entry)
        return {
            f"waiting_on_{owner.actor_key()}": waiting, "jobs": jobs,
            "answer_with": {"tool": "answer_questions", "note": f"Read {owner.name()} each card and get {owner.poss_adj()} words first; the "
                            "star marks the recommended default.",
                            "example": {"job_id": "<job_id>", "answers": [
                                {"qid": "<qid>", "option": "<key or recommended>", "note": "optional"}]}},
        }

    def answer_questions(arguments: dict, token_info: mcp_auth.TokenInfo) -> dict:
        _require(arguments, "job_id")
        job_id = arguments["job_id"]
        _known_job(job_id)
        human_only = [k for k in ("and_continue", "then_accept", "write_source") if arguments.get(k)]
        if human_only:
            raise ToolError(
                INVALID_PARAMS,
                f"{', '.join(human_only)} is human-only: use send_back_job to send the job back or accept_job to accept it")
        answers = arguments.get("answers") or []
        accept_recommended = bool(arguments.get("accept_recommended"))
        done = bool(arguments.get("done"))
        if not isinstance(answers, list):
            raise ToolError(INVALID_PARAMS, "answers must be a list")
        if not answers and not accept_recommended and not done:
            raise ToolError(INVALID_PARAMS, "give answers, accept_recommended: true, or done: true")
        if len(answers) > _Q_MAX_ANSWERS:
            raise ToolError(INVALID_PARAMS, f"at most {_Q_MAX_ANSWERS} answers per call")
        clean = []
        for i, a in enumerate(answers):
            where = f"answers[{i}]"
            if not isinstance(a, dict):
                raise ToolError(INVALID_PARAMS, f"{where} must be an object")
            qid = a.get("qid")
            if not isinstance(qid, str) or not qid.strip():
                raise ToolError(INVALID_PARAMS, f"{where} needs a qid")
            chosen = [k for k in ("option", "other") if a.get(k) is not None] + (["defer"] if a.get("defer") else [])
            if len(chosen) != 1:
                raise ToolError(INVALID_PARAMS, f"{where} needs exactly one of option, other or defer")
            item: dict = {"qid": qid.strip()[:300]}
            if chosen[0] == "option":
                if not isinstance(a["option"], str) or not a["option"].strip():
                    raise ToolError(INVALID_PARAMS, f"{where}.option must be an option key or recommended")
                item["option"] = a["option"].strip()[:40]
            elif chosen[0] == "other":
                if not isinstance(a["other"], str) or not a["other"].strip():
                    raise ToolError(INVALID_PARAMS, f"{where}.other must be non-empty text")
                item["other"] = a["other"].strip()[:questions_module.OTHER_CAP]
            else:
                item["defer"] = True
            if a.get("note") is not None:
                item["note"] = str(a["note"])[:questions_module.NOTE_CAP]
            clean.append(item)
        qids = arguments.get("qids")
        if qids is not None:
            if not isinstance(qids, list) or not all(isinstance(q, str) for q in qids):
                raise ToolError(INVALID_PARAMS, "qids must be a list of strings")
            qids = [q[:300] for q in qids][:_Q_MAX_ANSWERS * 4]
        args = {"job_id": job_id, "answers": clean, "accept_recommended": accept_recommended,
                "qids": qids or None, "rescan": False, "done": done}
        return _write("answer", args, arguments)

    job_id_schema = {"type": "string", "description": "Steward job id"}
    request_id_schema = {"type": "string", "description": "optional idempotency key"}

    # Tool descriptions name the owner; built per call of build_tools (never at import).
    _o, _op, _s, _ob, _pa, _v = owner.name(), owner.poss(), owner.subj(), owner.obj(), owner.poss_adj(), owner.verb
    return [
        ToolSpec(
            name="list_jobs",
            description=(
                "List jobs, optionally filtered by state. Running jobs carry `eta` (p10_min/p50_min/p90_min, shown, units), "
                "`eta_p50_min` and `eta_text`: the range is the answer; p50 is null until the job has finished units; "
                "eta_text is the human phrasing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "states": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(_ALLOWED_STATES)},
                    }
                },
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=list_jobs,
        ),
        ToolSpec(
            name="get_job",
            description=(
                "Full job detail: contract, active constraints, revision, herdr location, and `eta` for a running job "
                "(the range is the answer; p50 is null until the job has finished units; `shown` is the human phrasing, "
                "the same text as eta_text in list_jobs)."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_job,
        ),
        ToolSpec(
            name="get_current_step",
            description=(
                "Current heartbeat class, next contract step, pending steering receipts, and `eta` for a running job: "
                "the range is the answer; p50 is null until the job has finished units; `shown` is the human phrasing "
                "(eta_text in list_jobs)."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_current_step,
        ),
        ToolSpec(
            name="get_recent_events",
            description="Recent audit-trail events for a job.",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": job_id_schema,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                },
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_recent_events,
        ),
        ToolSpec(
            name="get_verification",
            description="Latest verification result: commit, new/fixed/pre-existing failures, weakening.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_verification,
        ),
        ToolSpec(
            name="get_diff_summary",
            description="git diff --stat and changed file list against the job's baseline commit.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_diff_summary,
        ),
        ToolSpec(
            name="explain_blocker",
            description="Why a job isn't progressing right now, if anything is blocking it.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=explain_blocker,
        ),
        ToolSpec(
            name="explain_job",
            description=(
                "A standard, deterministic explanation of one job (no model): what the worker is doing now, progress "
                "(units, current unit, ETA), the last 3 milestones, what it is blocked or waiting on (approvals, questions, "
                "conflicts) or 'nothing', spend so far and per hour, and health flags. `text` is the ready-to-read form."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=explain_job,
        ),
        ToolSpec(
            name="steer_job",
            description=(
                "Steer a running job. Requires expected_revision (optimistic concurrency: "
                "a stale value returns a CONFLICT receipt naming current_revision) and "
                "human_instruction (verbatim); instruction is what is actually delivered. "
                f"Set urgent=true only when {_o} says it cannot wait."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": job_id_schema,
                    "expected_revision": {"type": "integer"},
                    "instruction": {
                        "type": "string",
                        "description": "interpreted instruction to deliver",
                    },
                    "human_instruction": {"type": "string", "description": "verbatim human wording"},
                    "verb": {
                        "type": "string",
                        "enum": ["nudge", "focus", "avoid", "goal", "priority"],
                        "default": "nudge",
                    },
                    "urgent": {
                        "type": "boolean",
                        "default": False,
                        "description": "interrupt the worker now instead of waiting for its next natural pause",
                    },
                    "request_id": request_id_schema,
                },
                "required": ["job_id", "expected_revision", "instruction", "human_instruction"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:steer",
            handler=steer_job,
        ),
        ToolSpec(
            name="pause_job",
            description="Pause a running job.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:pause",
            handler=pause_job,
        ),
        ToolSpec(
            name="resume_job",
            description="Resume a paused job.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:pause",
            handler=resume_job,
        ),
        ToolSpec(
            name="cancel_job",
            description="Stop a job. Not reversible for the current worker attempt.",
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:cancel",
            handler=cancel_job,
        ),
        ToolSpec(
            name="approve_action",
            description="Approve or deny an open human-judgment approval (e.g. acceptance).",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": job_id_schema,
                    "approval_id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "deny"],
                        "default": "approve",
                    },
                    "request_id": request_id_schema,
                },
                "required": ["job_id", "approval_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
            required_scope="runner:approve",
            handler=approve_action,
        ),
        ToolSpec(
            name="get_supervisors",
            description=(
                "Show who is supervising a job: the worker plus the advisor, doctor, observer and any custom watchers, "
                f"with each one's model, on/off switch, current status and tokens/dollars spent. Use when {_o} asks 'who is watching this job' or what a supervisor is doing."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_supervisors,
        ),
        ToolSpec(
            name="get_watchers_feed",
            description=(
                "Plain-text activity feed of what the supervisors and watchers have said or done on a job, newest last. "
                "Use to answer 'what has the advisor/doctor/watchers been up to' or to see why a supervisor nudged or escalated."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "last": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_watchers_feed,
        ),
        ToolSpec(
            name="get_tokens",
            description=(
                "Token usage and dollar cost, per job and in total: total, last hour, cached vs fresh vs output, effective tokens, USD, "
                "and a per-source breakdown (worker, advisor, doctor, watchers). Omit job_id to cover all active jobs. Use for 'how much is this costing'."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "optional; omit for all active jobs"}},
                "required": [],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_tokens,
        ),
        ToolSpec(
            name="get_worker_activity",
            description=(
                "What the worker actually said, ran and saw, read from its session transcript (secrets redacted): recent turns "
                "(said / tool / result / error / steer / prompt), a one-line 'now' (last tool, waiting on a result or idle), model and last activity. "
                "Use for 'what is the worker doing right now', 'why is it stuck' and 'did it take my steer' (steers show as STEER rev N). "
                "Returns the structured turns plus a plain-text rendering in `text`. `last` = turns from the end (default 10); "
                "`since` = an ISO timestamp or the previous call's next_since cursor to page forward."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": job_id_schema,
                    "last": {"type": "integer", "description": "turns from the end (default 10)"},
                    "since": {"type": ["string", "integer"], "description": "optional ISO timestamp or turn-index cursor (next_since)"},
                },
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_worker_activity,
        ),
        ToolSpec(
            name="get_decision",
            description=(
                f"What is waiting on {_o} right now: a finished job to accept or reject, a worker permission to approve or deny, or a watcher escalation. "
                "Returns the evidence (tests, commits, what the worker said), the approval id and the exact tools to call for each choice. "
                f"Omit job_id to check every active job. Use this first when {_o} asks 'what needs me' or before accepting anything."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "optional; omit for all active jobs"}},
                "required": [],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_decision,
        ),
        ToolSpec(
            name="preview_incorporate",
            description=(
                "Dry run of merging an accepted job's work into the source repo: commits, diffstat, files touched, uncommitted-file overlap and predicted conflicts. "
                f"Changes nothing. Always show this to {_o} before calling incorporate_job."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=preview_incorporate,
        ),
        ToolSpec(
            name="accept_job",
            description=(
                f"Accept a finished job (its work meets the finish line). {_op} judgment call: only when {_s} clearly {_v('says')} accept, "
                f"ideally after you have read {_ob} get_decision's evidence. Does not merge the code; see preview_incorporate and incorporate_job for that."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:approve",
            handler=accept_job,
        ),
        ToolSpec(
            name="reject_job",
            description=(
                "Reject a finished job's acceptance request: the work is not good enough and goes back for another round. "
                f"Only when {_o} clearly says reject. Fails if the job has no pending acceptance."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:approve",
            handler=reject_job,
        ),
        ToolSpec(
            name="send_back_job",
            description=(
                "Send a finished-but-unaccepted job (NEEDS_HUMAN) back to work with new instructions: it continues in the same worktree/branch "
                "(its commits stay) with a fresh worker that sees the goal, all prior steering and your instructions. Unlike reject_job this does NOT stop the job. "
                f"Always confirm the exact instructions with {_o} first and only call when {_s} clearly {_v('says')} send it back. "
                "Set goal_append=true to also append them to the job's goal/contract (a change of what 'done' means); the default is a focus steer."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "instructions": {"type": "string", "description": "what the worker should do now"},
                               "goal_append": {"type": "boolean", "default": False}, "request_id": request_id_schema},
                "required": ["job_id", "instructions"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:approve",
            handler=send_back_job,
        ),
        ToolSpec(
            name="release_job",
            description=(
                "Release an accepted job: clear it from the active list and close its tab. Its accepted work can still be incorporated later. "
                f"Only when {_o} says {_s} {_v('is')} done with the job."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:approve",
            handler=release_job,
        ),
        ToolSpec(
            name="incorporate_job",
            description=(
                "Merge an accepted job's branch into the source repo (git merge, never forced). Changes real code: call preview_incorporate first, "
                f"show {_o} the result and get {_pa} explicit go. Set release=true to also release the job after a successful merge."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "release": {"type": "boolean", "default": False}, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:approve",
            handler=incorporate_job,
        ),
        ToolSpec(
            name="toggle_supervisor",
            description=(
                "Turn a supervisor on/off for one job. role is advisor, doctor or observer (old names astra/sol/jev also work) or the name of a custom watcher. "
                "state is optional: omit to flip to the next state; the advisor cycles off/shadow/active, the others on/off. This only ever narrows or restores defaults."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "role": {"type": "string", "description": "advisor, doctor, observer, or a custom watcher name"}, "state": {"type": "string", "enum": ["on", "off", "shadow", "active", "toggle"], "description": "optional; default toggle"}, "request_id": request_id_schema},
                "required": ["job_id", "role"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:steer",
            handler=toggle_supervisor,
        ),
        ToolSpec(
            name="add_watcher",
            description=(
                f"Attach a custom watcher to a job: a small model that periodically checks a question you write (ask) and can nudge the worker or escalate to {_o}. "
                f"every is a duration like 10m; may lists its powers (nudge, escalate). Use when {_o} says 'keep an eye on X'."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "name": {"type": "string", "description": "lowercase letters, digits, - or _"}, "ask": {"type": "string", "description": "what the watcher should check each time"}, "model": {"type": "string", "default": "haiku"}, "every": {"type": "string", "default": "10m"}, "may": {"type": "array", "items": {"type": "string", "enum": ["nudge", "escalate"]}, "default": []}, "request_id": request_id_schema},
                "required": ["job_id", "name", "ask"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:steer",
            handler=add_watcher,
        ),
        ToolSpec(
            name="remove_watcher",
            description=(
                "Remove a custom watcher from a job."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "name": {"type": "string"}, "request_id": request_id_schema},
                "required": ["job_id", "name"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": True},
            required_scope="runner:steer",
            handler=remove_watcher,
        ),
        ToolSpec(
            name="create_job",
            description=(
                "Create a new job (does NOT start it): Steward inspects the repo and drafts a contract with a goal, boundaries and done-when checks. "
                f"The result includes contract_text: read it back to {_o} in plain language and ask for {_pa} explicit go. "
                f"Only after {_s} {_v('says')} go, call launch_job. repo is a path on the Steward machine; finish_line is an optional plain-language definition of done."
            ),
            input_schema={
                "type": "object",
                "properties": {"goal": {"type": "string"}, "repo": {"type": "string", "description": "absolute path of the repo the job works on"}, "finish_line": {"type": "string"}, "watchers": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "ask": {"type": "string"}, "model": {"type": "string"}, "every": {"type": "string"}, "may": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "ask"]}}, "research": {"type": "boolean", "description": "give the worker the sanctioned research MCP (spend-capped)"}, "grant_keys": {"type": "array", "items": {"type": "string"}, "description": "API key names (e.g. SCRAPECREATORS_API_KEY) to put in the worker's environment"}, "research_cap": {"type": "number", "description": "research spend cap in USD (default 5)"}, "follow_up": {"type": "string", "description": "id of a finished (ACCEPTED/RELEASED) job this one continues; repo defaults to its repo"}, "request_id": request_id_schema},
                "required": ["goal", "repo"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:approve",
            handler=create_job,
        ),
        ToolSpec(
            name="get_result",
            description=(
                "One readable report on a job: goal, finish line, state, verdict/evidence, the worker's final message, "
                "changed files, branch, merge commit, cost, follow-up links and the next commands that apply."
            ),
            input_schema={"type": "object", "properties": {"job_id": job_id_schema}, "required": ["job_id"]},
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_result,
        ),
        ToolSpec(
            name="get_questions",
            description=(
                f"The questions a job's worker left for {_o} (gray areas, design choices), as cards: a title, the question, options with the "
                "recommended one first and starred, a rationale, and whether it blocks work (blocking questions come first). Read-only. "
                "Omit job_id to see every active job that has questions waiting; include 'all' also shows answered and deferred ones; "
                f"round pages four cards at a time. Read the cards to {_o}, then call answer_questions with what {_s} {_v('says')}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "optional; omit for every active job with questions waiting"},
                    "include": {"type": "string", "enum": ["waiting", "all"], "default": "waiting"},
                    "round": {"type": "integer", "minimum": 1, "description": "page of four cards"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
                },
                "required": [],
            },
            annotations={"readOnlyHint": True},
            required_scope="runner:read",
            handler=get_questions,
        ),
        ToolSpec(
            name="answer_questions",
            description=(
                f"Record {_op} answers to a job's waiting questions. Only call this with {_op} own words: read {_ob} the card from get_questions "
                f"first (blocking questions come first), then pass {_pa} choice per question: option (a key, or 'recommended' for the starred default), "
                f"other ({_pa} own words) or defer:true, with an optional note. accept_recommended:true answers every OPEN STARRED card with its "
                f"recommended default (cards without a star are left for {_ob}), so confirm that with {_ob} first. done:true writes the batch to "
                "the job now instead of waiting for the quiet period. At most 15 answers per call. This tool cannot send the job back, accept it or "
                "write the source branch: use send_back_job or accept_job for those."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": job_id_schema,
                    "answers": {
                        "type": "array", "maxItems": _Q_MAX_ANSWERS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "qid": {"type": "string", "description": "the card's qid or its anchor, e.g. G4"},
                                "option": {"type": "string", "description": "an option key, or 'recommended'"},
                                "other": {"type": "string", "description": f"{_op} own answer in {_pa} words"},
                                "defer": {"type": "boolean"},
                                "note": {"type": "string", "description": f"optional note from {_o}"},
                            },
                            "required": ["qid"],
                        },
                    },
                    "accept_recommended": {"type": "boolean", "default": False},
                    "qids": {"type": "array", "items": {"type": "string"}, "description": "limit accept_recommended to these cards"},
                    "done": {"type": "boolean", "default": False},
                    "request_id": request_id_schema,
                },
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:steer",
            handler=answer_questions,
        ),
        ToolSpec(
            name="launch_job",
            description=(
                "Start a created job: opens its Herdr tab and begins a worker running with real budget. "
                f"Never call this until you have read the contract (from create_job or get_job) back to {_o} and {_s} {_v('has')} explicitly said go. "
                "model picks the worker model (default sonnet)."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "model": {"type": "string", "default": "sonnet"}, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:approve",
            handler=launch_job,
        ),
        ToolSpec(
            name="play_job",
            description=(
                "Launch a staged job (created, contract ready, never launched) exactly like launch_job, using the launch "
                "settings recorded on the job if any, else the default sonnet worker. Does nothing if it is already launched."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": job_id_schema, "request_id": request_id_schema},
                "required": ["job_id"],
            },
            annotations={"readOnlyHint": False, "destructiveHint": False},
            required_scope="runner:approve",
            handler=play_job,
        ),
    ]
