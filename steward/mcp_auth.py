"""Bearer-token auth with scopes for the MCP server (MCP-02, SP-29, SP-30).

Token store: `$STEWARD_HOME/mcp_tokens.json` (mode 0600) — a JSON list of
entries `{id, token_hash (sha256 hex of the plaintext), scopes, created_at,
revoked}`. The plaintext token is never written to disk: `create_token`
returns it exactly once (the caller must show/save it there); every check
after that hashes the presented token and compares it against the stored
hash with a constant-time compare (`hmac.compare_digest`).

Scopes: `runner:read`, `runner:steer`, `runner:pause`, `runner:cancel`,
`runner:approve`, `runner:admin`. A token created with no explicit scopes
defaults to `[runner:read]` (SP-30: the read path is the dependable one on
ChatGPT Pro today; ship read-only by default, opt into write scopes).

This module never touches `steward.api`/`steward.store`/the runner — it only
answers "is this bearer token valid, and does it carry this scope", which is
exactly the check `steward.mcp_server` needs before dispatching a tool call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from steward.store import resolve_home

SCOPES = (
    "runner:read",
    "runner:steer",
    "runner:pause",
    "runner:cancel",
    "runner:approve",
    "runner:admin",
)
DEFAULT_SCOPES = ("runner:read",)

_TOKENS_FILE = "mcp_tokens.json"
_FILE_MODE = 0o600


class Unauthorized(Exception):
    """No bearer token, or one that doesn't match any live entry."""


class InsufficientScope(Exception):
    """A valid, live token that lacks the scope a tool call requires."""

    def __init__(self, required_scope: str):
        super().__init__(f"insufficient_scope: requires {required_scope}")
        self.required_scope = required_scope


@dataclass(frozen=True)
class TokenInfo:
    id: str
    scopes: tuple[str, ...]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tokens_path(home: Path | str | None) -> Path:
    return resolve_home(home) / _TOKENS_FILE


def _load(home: Path | str | None) -> list[dict]:
    path = _tokens_path(home)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _save(home: Path | str | None, entries: list[dict]) -> None:
    path = _tokens_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))
    os.chmod(path, _FILE_MODE)


def create_token(scopes: list[str] | None = None, *, home: Path | str | None = None) -> tuple[str, dict]:
    """Create and persist a new token; return `(plaintext, entry)`.

    `plaintext` is shown here and only here — the persisted `entry` carries
    just its sha256 hash.
    """
    scopes = list(scopes) if scopes else list(DEFAULT_SCOPES)
    for scope in scopes:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")

    plaintext = secrets.token_urlsafe(32)
    entry = {
        "id": secrets.token_hex(8),
        "token_hash": _hash(plaintext),
        "scopes": scopes,
        "created_at": _now_iso(),
        "revoked": False,
    }
    entries = _load(home)
    entries.append(entry)
    _save(home, entries)
    return plaintext, entry


def list_tokens(*, home: Path | str | None = None) -> list[dict]:
    """Entries only — never the plaintext, which was never stored."""
    return _load(home)


def revoke(token_id: str, *, home: Path | str | None = None) -> bool:
    entries = _load(home)
    found = False
    for entry in entries:
        if entry["id"] == token_id:
            entry["revoked"] = True
            found = True
    if found:
        _save(home, entries)
    return found


def _extract_bearer(header: str | None) -> str:
    if not header or not header.startswith("Bearer "):
        raise Unauthorized("missing or malformed Authorization header")
    token = header[len("Bearer "):].strip()
    if not token:
        raise Unauthorized("empty bearer token")
    return token


def authorize(
    header: str | None,
    required_scope: str | None = None,
    *,
    home: Path | str | None = None,
) -> TokenInfo:
    """Validate an `Authorization: Bearer <token>` header.

    Raises `Unauthorized` when there is no header, or no live (non-revoked)
    token matches; raises `InsufficientScope` when a matching token is live
    but lacks `required_scope`. Returns `TokenInfo` on success.
    """
    token = _extract_bearer(header)
    token_hash = _hash(token)

    match: TokenInfo | None = None
    for entry in _load(home):
        if entry.get("revoked"):
            continue
        if hmac.compare_digest(entry["token_hash"], token_hash):
            match = TokenInfo(id=entry["id"], scopes=tuple(entry["scopes"]))
            break

    if match is None:
        raise Unauthorized("token not recognized or revoked")
    if required_scope is not None and required_scope not in match.scopes:
        raise InsufficientScope(required_scope)
    return match
