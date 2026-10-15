"""SQLite state for Steward's OAuth 2.1 authorization server (LIVE-01, SP-29).

Separate database file `$STEWARD_HOME/oauth.db` (mode 0600, WAL) so OAuth
state never mixes with job state. Tables: `clients`, `auth_transactions`,
`auth_codes`, `access_tokens`, `refresh_tokens` (with `family_id`,
`rotated_from`, `revoked`).

Every secret (authorization code, access token, refresh token, CSRF value) is
stored only as its sha256 hex digest; plaintext never touches disk. The clock
is injectable (`clock() -> epoch seconds`) so expiry is tested without sleeps.

Concurrency: `ThreadingHTTPServer` calls in from many threads, so the store
holds one connection plus a lock, and every state transition that must be
atomic (consume a code and mint its tokens; rotate a refresh token; detect
reuse and revoke a family) runs inside one `BEGIN IMMEDIATE` transaction
under that lock. Adapted from herdr-gpt's oauth_store (read, re-implemented;
not imported).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from steward.store import resolve_home

DB_FILENAME = "oauth.db"
_FILE_MODE = 0o600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_transactions (
    tx_id TEXT PRIMARY KEY,
    csrf_digest TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES clients(client_id),
    redirect_uri TEXT NOT NULL,
    redirect_explicit INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    state TEXT,
    resource TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    failed_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_digest TEXT PRIMARY KEY,
    tx_id TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES clients(client_id),
    redirect_uri TEXT NOT NULL,
    redirect_explicit INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    family_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token_digest TEXT PRIMARY KEY,
    token_id TEXT NOT NULL UNIQUE,
    family_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_digest TEXT PRIMARY KEY,
    token_id TEXT NOT NULL UNIQUE,
    family_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    rotated_from TEXT,
    used_at INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS access_family ON access_tokens(family_id);
CREATE INDEX IF NOT EXISTS refresh_family ON refresh_tokens(family_id);
"""


def digest(secret: str) -> str:
    """sha256 hex of a secret — the only form any secret is stored in."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Outcome:
    """Result of an atomic token transition.

    status: "ok" (tokens minted), "reused" (replay detected; family revoked),
    or "invalid" (unknown/expired/revoked/wrong client; nothing changed).
    """

    status: str
    scopes: tuple[str, ...] = ()
    family_id: str | None = None


def _secure_create(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
    os.close(fd)
    os.chmod(path, _FILE_MODE)


class OAuthStore:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        home: Path | str | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.path = Path(path) if path is not None else resolve_home(home) / DB_FILENAME
        self.clock = clock or time.time
        _secure_create(self.path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        for suffix in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            if side.exists():
                os.chmod(side, _FILE_MODE)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def now(self) -> int:
        return int(self.clock())

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _one(self, sql: str, args: tuple) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    # -- clients ------------------------------------------------------------

    def register_client(
        self, client_id: str, client_name: str, redirect_uris: list[str], scopes: list[str]
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO clients VALUES (?, ?, ?, ?, ?)",
                (client_id, client_name, json.dumps(redirect_uris), " ".join(scopes), self.now()),
            )

    def get_client(self, client_id: str) -> dict | None:
        row = self._one("SELECT * FROM clients WHERE client_id=?", (client_id,))
        if row is not None:
            row["redirect_uris"] = json.loads(row["redirect_uris"])
        return row

    def evict_idle_client(self) -> bool:
        """Drop the oldest client that holds no tokens (with its dead
        transactions/codes), so open DCR can't fill the client cap and lock
        the owner out of linking. Clients with live grants are never evicted."""
        with self._tx() as c:
            row = c.execute(
                "SELECT client_id FROM clients c WHERE"
                " NOT EXISTS (SELECT 1 FROM access_tokens a WHERE a.client_id=c.client_id)"
                " AND NOT EXISTS (SELECT 1 FROM refresh_tokens r WHERE r.client_id=c.client_id)"
                " ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            cid = row["client_id"]
            c.execute("DELETE FROM auth_codes WHERE client_id=?", (cid,))
            c.execute("DELETE FROM auth_transactions WHERE client_id=?", (cid,))
            c.execute("DELETE FROM clients WHERE client_id=?", (cid,))
            return True

    def count_clients(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM clients").fetchone()[0]

    # -- authorization transactions -------------------------------------------

    def create_transaction(
        self,
        *,
        tx_id: str,
        csrf_digest: str,
        client_id: str,
        redirect_uri: str,
        redirect_explicit: bool,
        code_challenge: str,
        scopes: list[str],
        state: str | None,
        resource: str,
        ttl: int,
    ) -> None:
        now = self.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO auth_transactions (tx_id, csrf_digest, client_id, redirect_uri,"
                " redirect_explicit, code_challenge, scopes, state, resource, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tx_id, csrf_digest, client_id, redirect_uri, int(redirect_explicit),
                    code_challenge, " ".join(scopes), state, resource, now, now + ttl,
                ),
            )

    def count_open_transactions(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT count(*) FROM auth_transactions WHERE consumed_at IS NULL AND expires_at > ?",
                (self.now(),),
            ).fetchone()[0]

    def get_transaction(self, tx_id: str) -> dict | None:
        return self._one("SELECT * FROM auth_transactions WHERE tx_id=?", (tx_id,))

    def record_failed_attempt(self, tx_id: str, max_attempts: int) -> int:
        """Count a wrong passphrase; burn the transaction once `max_attempts` is hit."""
        with self._tx() as c:
            c.execute(
                "UPDATE auth_transactions SET failed_attempts = failed_attempts + 1 WHERE tx_id=?",
                (tx_id,),
            )
            n = c.execute(
                "SELECT failed_attempts FROM auth_transactions WHERE tx_id=?", (tx_id,)
            ).fetchone()[0]
            if n >= max_attempts:
                c.execute(
                    "UPDATE auth_transactions SET consumed_at=? WHERE tx_id=? AND consumed_at IS NULL",
                    (self.now(), tx_id),
                )
            return n

    def consume_transaction(self, tx_id: str) -> bool:
        """Atomically mark an open, unexpired transaction consumed (exactly once)."""
        now = self.now()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE auth_transactions SET consumed_at=?"
                " WHERE tx_id=? AND consumed_at IS NULL AND expires_at > ?",
                (now, tx_id, now),
            )
            return cur.rowcount == 1

    # -- authorization codes ----------------------------------------------------

    def create_code(
        self,
        *,
        code_digest: str,
        tx_id: str,
        client_id: str,
        redirect_uri: str,
        redirect_explicit: bool,
        code_challenge: str,
        scopes: list[str],
        resource: str,
        family_id: str,
        ttl: int = 120,
    ) -> None:
        now = self.now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    code_digest, tx_id, client_id, redirect_uri, int(redirect_explicit),
                    code_challenge, " ".join(scopes), resource, family_id, now, now + ttl,
                ),
            )

    def get_code(self, code_digest: str) -> dict | None:
        return self._one("SELECT * FROM auth_codes WHERE code_digest=?", (code_digest,))

    def exchange_code(
        self,
        code_digest: str,
        *,
        access_digest: str,
        access_id: str,
        refresh_digest: str,
        refresh_id: str,
        access_ttl: int,
        refresh_ttl: int,
    ) -> Outcome:
        """Consume a code and mint its first access/refresh pair, atomically.

        A second presentation of the same code is a replay: the family minted
        from it is revoked (OAuth 2.1 §4.1.3) and nothing new is issued.
        Callers validate client/redirect/PKCE/expiry *before* calling this.
        """
        now = self.now()
        with self._tx() as c:
            row = c.execute("SELECT * FROM auth_codes WHERE code_digest=?", (code_digest,)).fetchone()
            if row is None:
                return Outcome("invalid")
            if row["used_at"] is not None:
                self._revoke_family(c, row["family_id"])
                return Outcome("reused", family_id=row["family_id"])
            if now >= row["expires_at"]:
                return Outcome("invalid")
            c.execute("UPDATE auth_codes SET used_at=? WHERE code_digest=?", (now, code_digest))
            self._insert_pair(
                c, row, now, access_digest, access_id, refresh_digest, refresh_id,
                access_ttl, refresh_ttl, rotated_from=None,
            )
            return Outcome("ok", tuple(row["scopes"].split()), row["family_id"])

    # -- tokens -----------------------------------------------------------------

    @staticmethod
    def _insert_pair(
        c, src, now, access_digest, access_id, refresh_digest, refresh_id,
        access_ttl, refresh_ttl, *, rotated_from,
    ) -> None:
        c.execute(
            "INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (access_digest, access_id, src["family_id"], src["client_id"], src["scopes"],
             src["resource"], now, now + access_ttl),
        )
        c.execute(
            "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (refresh_digest, refresh_id, src["family_id"], src["client_id"], src["scopes"],
             src["resource"], now, now + refresh_ttl, rotated_from),
        )

    @staticmethod
    def _revoke_family(c, family_id: str) -> None:
        c.execute("UPDATE access_tokens SET revoked=1 WHERE family_id=?", (family_id,))
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE family_id=?", (family_id,))

    def rotate_refresh(
        self,
        refresh_digest_presented: str,
        *,
        client_id: str,
        access_digest: str,
        access_id: str,
        refresh_digest: str,
        refresh_id: str,
        access_ttl: int,
        refresh_ttl: int,
        scopes: list[str] | None = None,
    ) -> Outcome:
        """Rotate a refresh token. Presenting an already-rotated token revokes
        its whole family (reuse detection); the replacement pair keeps the
        family id and records `rotated_from`."""
        now = self.now()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM refresh_tokens WHERE token_digest=?", (refresh_digest_presented,)
            ).fetchone()
            if row is None or row["client_id"] != client_id:
                return Outcome("invalid")
            if row["revoked"]:
                return Outcome("invalid", family_id=row["family_id"])
            if row["used_at"] is not None:
                self._revoke_family(c, row["family_id"])
                return Outcome("reused", family_id=row["family_id"])
            if now >= row["expires_at"]:
                return Outcome("invalid")
            src = dict(row)
            if scopes is not None:
                src["scopes"] = " ".join(scopes)
            c.execute("UPDATE refresh_tokens SET used_at=? WHERE token_digest=?", (now, row["token_digest"]))
            c.execute("UPDATE access_tokens SET revoked=1 WHERE family_id=?", (row["family_id"],))
            self._insert_pair(
                c, src, now, access_digest, access_id, refresh_digest, refresh_id,
                access_ttl, refresh_ttl, rotated_from=row["token_id"],
            )
            return Outcome("ok", tuple(src["scopes"].split()), row["family_id"])

    def get_access(self, access_digest: str) -> dict | None:
        return self._one("SELECT * FROM access_tokens WHERE token_digest=?", (access_digest,))

    def get_refresh(self, refresh_digest: str) -> dict | None:
        return self._one("SELECT * FROM refresh_tokens WHERE token_digest=?", (refresh_digest,))

    def revoke_by_token(self, token_digest: str, client_id: str | None = None) -> bool:
        """RFC 7009: revoking either token of a family revokes the whole family."""
        with self._tx() as c:
            for table in ("refresh_tokens", "access_tokens"):
                row = c.execute(
                    f"SELECT family_id, client_id FROM {table} WHERE token_digest=?", (token_digest,)
                ).fetchone()
                if row is not None:
                    if client_id is not None and row["client_id"] != client_id:
                        return False
                    self._revoke_family(c, row["family_id"])
                    return True
        return False

    def revoke_all(self) -> int:
        """Kill switch: revoke every token family (clients stay registered)."""
        with self._tx() as c:
            n = c.execute("UPDATE access_tokens SET revoked=1 WHERE revoked=0").rowcount
            n += c.execute("UPDATE refresh_tokens SET revoked=1 WHERE revoked=0").rowcount
            return n

    # -- housekeeping -------------------------------------------------------------

    def purge_expired(self) -> None:
        """Drop dead transactions/codes (bounded growth under open DCR/authorize)."""
        now = self.now()
        with self._tx() as c:
            c.execute("DELETE FROM auth_transactions WHERE expires_at <= ?", (now,))
            # keep used codes a while so a replay can still find (and revoke) its family
            c.execute("DELETE FROM auth_codes WHERE expires_at <= ?", (now - 86400,))
