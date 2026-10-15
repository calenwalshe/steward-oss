"""OAuth state store: digest-only, 0600, WAL, atomic code/refresh transitions (LIVE-01)."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from steward import oauth_store
from steward.oauth_store import OAuthStore, digest


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = OAuthStore(tmp_path / "oauth.db", clock=clock)
    yield s
    s.close()


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_db_file_is_0600_and_wal(tmp_path, clock):
    path = tmp_path / "sub" / "oauth.db"
    s = OAuthStore(path, clock=clock)
    try:
        assert _mode(path) == 0o600
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        s.register_client("c1", "ChatGPT", ["https://chatgpt.com/cb"], ["runner:read"])
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                assert _mode(side) & 0o077 == 0
    finally:
        s.close()


def test_default_path_is_under_steward_home(steward_home, clock):
    s = OAuthStore(clock=clock)
    try:
        assert s.path == steward_home / "oauth.db"
    finally:
        s.close()


def test_tables_exist(store):
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"clients", "auth_transactions", "auth_codes", "access_tokens", "refresh_tokens"} <= names


def test_digest_is_sha256_hex():
    d = digest("abc")
    assert len(d) == 64 and d == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _issue(store, family="fam1", access="acc-plain", refresh="ref-plain", scopes=("runner:read",)):
    store.register_client("c1", "ChatGPT", ["https://chatgpt.com/cb"], list(scopes))
    store.create_code(
        code_digest=digest("code-plain"),
        tx_id="tx1",
        client_id="c1",
        redirect_uri="https://chatgpt.com/cb",
        redirect_explicit=True,
        code_challenge="x" * 43,
        scopes=list(scopes),
        resource="https://example.test/mcp",
        family_id=family,
    )
    outcome = store.exchange_code(
        digest("code-plain"),
        access_digest=digest(access),
        access_id="aid1",
        refresh_digest=digest(refresh),
        refresh_id="rid1",
        access_ttl=3600,
        refresh_ttl=30 * 86400,
    )
    return outcome


def test_no_plaintext_is_stored(store, tmp_path):
    _issue(store, access="SECRET-ACCESS-TOKEN", refresh="SECRET-REFRESH-TOKEN")
    store.close()
    raw = b""
    for p in tmp_path.iterdir():
        raw += p.read_bytes()
    assert b"SECRET-ACCESS-TOKEN" not in raw
    assert b"SECRET-REFRESH-TOKEN" not in raw
    assert b"code-plain" not in raw


def test_code_is_single_use_and_reuse_revokes_family(store):
    out = _issue(store)
    assert out.status == "ok"
    assert store.get_access(digest("acc-plain"))["revoked"] == 0
    again = store.exchange_code(
        digest("code-plain"),
        access_digest=digest("acc2"),
        access_id="aid2",
        refresh_digest=digest("ref2"),
        refresh_id="rid2",
        access_ttl=3600,
        refresh_ttl=30 * 86400,
    )
    assert again.status == "reused"
    assert store.get_access(digest("acc-plain"))["revoked"] == 1
    assert store.get_access(digest("acc2")) is None


def test_refresh_rotation_and_reuse_revokes_family(store, clock):
    _issue(store)
    r1 = store.rotate_refresh(
        digest("ref-plain"),
        client_id="c1",
        access_digest=digest("acc-B"),
        access_id="aidB",
        refresh_digest=digest("ref-B"),
        refresh_id="ridB",
        access_ttl=3600,
        refresh_ttl=30 * 86400,
    )
    assert r1.status == "ok"
    new_row = store._conn.execute(
        "SELECT rotated_from, family_id FROM refresh_tokens WHERE token_digest=?", (digest("ref-B"),)
    ).fetchone()
    assert new_row[0] == "rid1" and new_row[1] == "fam1"
    # prior access token of the family is retired on rotation
    assert store.get_access(digest("acc-plain"))["revoked"] == 1
    assert store.get_access(digest("acc-B"))["revoked"] == 0

    # reuse of the rotated (old) refresh token revokes the whole family
    r2 = store.rotate_refresh(
        digest("ref-plain"),
        client_id="c1",
        access_digest=digest("acc-C"),
        access_id="aidC",
        refresh_digest=digest("ref-C"),
        refresh_id="ridC",
        access_ttl=3600,
        refresh_ttl=30 * 86400,
    )
    assert r2.status == "reused"
    assert store.get_access(digest("acc-B"))["revoked"] == 1
    refresh_rows = store._conn.execute("SELECT revoked FROM refresh_tokens WHERE family_id='fam1'").fetchall()
    assert refresh_rows and all(r[0] == 1 for r in refresh_rows)
    assert store.get_access(digest("acc-C")) is None


def test_refresh_wrong_client_or_expired_is_refused(store, clock):
    _issue(store)
    store.register_client("c2", "Other", ["https://chatgpt.com/cb2"], ["runner:read"])
    out = store.rotate_refresh(
        digest("ref-plain"), client_id="c2", access_digest=digest("a"), access_id="a",
        refresh_digest=digest("r"), refresh_id="r", access_ttl=3600, refresh_ttl=100,
    )
    assert out.status == "invalid"
    clock.advance(30 * 86400 + 1)
    out = store.rotate_refresh(
        digest("ref-plain"), client_id="c1", access_digest=digest("a"), access_id="a",
        refresh_digest=digest("r"), refresh_id="r", access_ttl=3600, refresh_ttl=100,
    )
    assert out.status == "invalid"


def test_revoke_family_by_token(store):
    _issue(store)
    assert store.revoke_by_token(digest("ref-plain")) is True
    assert store.get_access(digest("acc-plain"))["revoked"] == 1
    assert store.revoke_by_token(digest("nope")) is False


def test_transaction_consume_is_atomic_once(store, clock):
    store.register_client("c1", "ChatGPT", ["https://chatgpt.com/cb"], ["runner:read"])
    store.create_transaction(
        tx_id="tx1", csrf_digest=digest("csrf"), client_id="c1",
        redirect_uri="https://chatgpt.com/cb", redirect_explicit=True, code_challenge="x" * 43,
        scopes=["runner:read"], state="s", resource="https://example.test/mcp", ttl=600,
    )
    assert store.get_transaction("tx1")["client_id"] == "c1"
    assert store.consume_transaction("tx1") is True
    assert store.consume_transaction("tx1") is False


def test_purge_removes_expired_transactions(store, clock):
    store.register_client("c1", "ChatGPT", ["https://chatgpt.com/cb"], ["runner:read"])
    store.create_transaction(
        tx_id="tx1", csrf_digest=digest("csrf"), client_id="c1",
        redirect_uri="https://chatgpt.com/cb", redirect_explicit=True, code_challenge="x" * 43,
        scopes=["runner:read"], state=None, resource="https://example.test/mcp", ttl=600,
    )
    clock.advance(601)
    store.purge_expired()
    assert store.get_transaction("tx1") is None


def test_state_survives_reopen(tmp_path, clock):
    s = OAuthStore(tmp_path / "oauth.db", clock=clock)
    _issue(s)
    s.close()
    s2 = OAuthStore(tmp_path / "oauth.db", clock=clock)
    try:
        assert s2.get_access(digest("acc-plain"))["revoked"] == 0
        assert s2.get_client("c1")["client_name"] == "ChatGPT"
    finally:
        s2.close()


def test_reopen_tightens_loose_permissions(tmp_path, clock):
    path = tmp_path / "oauth.db"
    OAuthStore(path, clock=clock).close()
    os.chmod(path, 0o644)
    s = OAuthStore(path, clock=clock)
    try:
        assert _mode(path) == 0o600
    finally:
        s.close()


def test_sqlite_integrity(store):
    assert isinstance(store._conn, sqlite3.Connection)
    assert oauth_store.DB_FILENAME == "oauth.db"
