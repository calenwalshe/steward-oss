"""Scoped bearer-token auth for the MCP server (MCP-02, SP-29, SP-30)."""

from __future__ import annotations

import json
import stat

import pytest

from steward import mcp_auth


def test_create_token_returns_plaintext_once_and_never_persists_it(steward_home):
    plaintext, entry = mcp_auth.create_token()

    assert plaintext
    assert entry["token_hash"] != plaintext
    on_disk = json.loads((steward_home / "mcp_tokens.json").read_text())
    assert len(on_disk) == 1
    assert "token_hash" in on_disk[0]
    assert plaintext not in json.dumps(on_disk)


def test_create_token_defaults_to_read_scope(steward_home):
    _, entry = mcp_auth.create_token()
    assert entry["scopes"] == ["runner:read"]


def test_create_token_rejects_unknown_scope(steward_home):
    with pytest.raises(ValueError):
        mcp_auth.create_token(["not_a_scope"])


def test_token_file_is_mode_0600(steward_home):
    mcp_auth.create_token()
    mode = stat.S_IMODE((steward_home / "mcp_tokens.json").stat().st_mode)
    assert mode == 0o600


def test_authorize_accepts_a_live_token_with_the_required_scope(steward_home):
    plaintext, entry = mcp_auth.create_token(["runner:read", "runner:steer"])

    info = mcp_auth.authorize(f"Bearer {plaintext}", "runner:steer")

    assert info.id == entry["id"]
    assert info.scopes == ("runner:read", "runner:steer")


def test_authorize_raises_unauthorized_for_missing_header(steward_home):
    with pytest.raises(mcp_auth.Unauthorized):
        mcp_auth.authorize(None, "runner:read")


def test_authorize_raises_unauthorized_for_malformed_header(steward_home):
    with pytest.raises(mcp_auth.Unauthorized):
        mcp_auth.authorize("Basic dXNlcjpwYXNz", "runner:read")


def test_authorize_raises_unauthorized_for_unknown_token(steward_home):
    mcp_auth.create_token()
    with pytest.raises(mcp_auth.Unauthorized):
        mcp_auth.authorize("Bearer not-a-real-token", "runner:read")


def test_authorize_raises_insufficient_scope_for_a_read_only_token_writing(steward_home):
    plaintext, _ = mcp_auth.create_token(["runner:read"])

    with pytest.raises(mcp_auth.InsufficientScope) as exc_info:
        mcp_auth.authorize(f"Bearer {plaintext}", "runner:steer")

    assert exc_info.value.required_scope == "runner:steer"


def test_revoke_makes_a_previously_valid_token_unauthorized(steward_home):
    plaintext, entry = mcp_auth.create_token()
    mcp_auth.authorize(f"Bearer {plaintext}", "runner:read")  # sanity: valid before revoke

    revoked = mcp_auth.revoke(entry["id"])

    assert revoked is True
    with pytest.raises(mcp_auth.Unauthorized):
        mcp_auth.authorize(f"Bearer {plaintext}", "runner:read")


def test_revoke_unknown_id_returns_false(steward_home):
    assert mcp_auth.revoke("no-such-id") is False


def test_list_tokens_never_exposes_plaintext(steward_home):
    plaintext, _ = mcp_auth.create_token()

    entries = mcp_auth.list_tokens()

    assert len(entries) == 1
    assert plaintext not in json.dumps(entries)
