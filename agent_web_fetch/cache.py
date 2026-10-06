"""SQLite cache, cross-process locks and per-domain request pacing.

The CLI runs as one process per request, so deduplication and per-domain
concurrency limits are coordinated through the same SQLite file using short
leases. Leases expire so a crashed process cannot wedge a URL forever.
"""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

DEFAULT_TTL = 3600
ERROR_TTL = 120  # short negative cache so an agent cannot hammer a failing URL

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    final_url TEXT,
    fetched_at REAL NOT NULL,
    backend TEXT NOT NULL,
    status INTEGER,
    title TEXT,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    url TEXT NOT NULL,
    mode TEXT NOT NULL,
    failed_at REAL NOT NULL,
    error TEXT NOT NULL,
    PRIMARY KEY (url, mode)
);
CREATE TABLE IF NOT EXISTS leases (
    key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (
    host TEXT PRIMARY KEY,
    next_allowed_at REAL NOT NULL DEFAULT 0,
    backoff REAL NOT NULL DEFAULT 0
);
"""


class Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        # Additive migration keeps existing pages, leases and holds intact. A write
        # transaction makes simultaneous first starts safe.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(domains)")}
            for name, definition in (("jina_delay", "REAL NOT NULL DEFAULT 0"),
                                     ("browser_delay", "REAL NOT NULL DEFAULT 1"),
                                     ("block_count", "INTEGER NOT NULL DEFAULT 0"),
                                     ("broken_until", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE domains ADD COLUMN {name} {definition}")
            self.db.execute("CREATE TABLE IF NOT EXISTS cooldowns (name TEXT PRIMARY KEY, until REAL NOT NULL)")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        self.owner = uuid.uuid4().hex

    def close(self) -> None:
        self.db.close()

    # -- pages -------------------------------------------------------------

    def get(self, url: str, mode: str = "auto", ttl: float = DEFAULT_TTL, newer_than: float = 0) -> Optional[dict]:
        row = self.db.execute(
            "SELECT url, final_url, fetched_at, backend, status, title, content FROM pages WHERE url = ?", (url,)
        ).fetchone()
        if not row:
            return None
        page = dict(zip(("url", "final_url", "fetched_at", "backend", "status", "title", "content"), row))
        if time.time() - page["fetched_at"] > ttl or page["fetched_at"] < newer_than:
            return None
        if mode != "auto" and page["backend"] != mode:
            return None
        return page

    def put(self, page: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO pages (url, final_url, fetched_at, backend, status, title, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (page["url"], page["final_url"], page.get("fetched_at", time.time()), page["backend"],
             page.get("status"), page.get("title"), page["content"]),
        )
        self.db.execute("DELETE FROM errors WHERE url = ?", (page["url"],))

    def get_error(self, url: str, mode: str, ttl: Optional[float] = None) -> Optional[dict]:
        rows = self.db.execute(
            "SELECT mode, failed_at, error FROM errors WHERE url = ? ORDER BY failed_at DESC", (url,)
        ).fetchall()
        # Browser hard blocks belong to the URL even if a caller changes mode.
        # Ordinary failures retain the original mode-specific cache behavior.
        for saved_mode, failed_at, payload in sorted(rows, key=lambda r: r[0] != mode):
            error = json.loads(payload)
            hard = self.hard_block(error)
            if saved_mode != mode and not hard:
                continue
            lifetime = ttl if ttl is not None else (1800 if hard else ERROR_TTL)
            if time.time() - failed_at <= lifetime:
                return error
        return None

    def put_error(self, url: str, mode: str, error: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO errors (url, mode, failed_at, error) VALUES (?, ?, ?, ?)",
            (url, mode, time.time(), json.dumps(error)),
        )

    def clear_error(self, url: str, mode: str) -> None:
        self.db.execute("DELETE FROM errors WHERE url = ? AND mode = ?", (url, mode))

    # -- leases (dedup + per-domain concurrency) ---------------------------

    def try_acquire(self, key: str, lease_seconds: float, reentrant: bool = True) -> bool:
        """Take or renew `key`. With reentrant=False even this owner cannot take a key it already holds."""
        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT owner, expires_at FROM leases WHERE key = ?", (key,)).fetchone()
            if row and row[1] > now and (row[0] != self.owner or not reentrant):
                self.db.execute("COMMIT")
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO leases (key, owner, expires_at) VALUES (?, ?, ?)",
                (key, self.owner, now + lease_seconds),
            )
            self.db.execute("COMMIT")
            return True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def renew(self, key: str, lease_seconds: float) -> None:
        self.db.execute("UPDATE leases SET expires_at = ? WHERE key = ? AND owner = ?",
                        (time.time() + lease_seconds, key, self.owner))

    def release(self, key: str) -> None:
        self.db.execute("DELETE FROM leases WHERE key = ? AND owner = ?", (key, self.owner))

    # -- per-domain pacing --------------------------------------------------

    def domain_wait(self, host: str) -> float:
        """Seconds until `host` may be contacted again."""
        row = self.db.execute("SELECT next_allowed_at FROM domains WHERE host = ?", (host,)).fetchone()
        return max(0.0, row[0] - time.time()) if row else 0.0

    def domain_backoff(self, host: str) -> float:
        row = self.db.execute("SELECT backoff FROM domains WHERE host = ?", (host,)).fetchone()
        return row[0] if row else 0.0

    def set_domain(self, host: str, delay: float, backoff: Optional[float] = None) -> None:
        """Hold off `host` for `delay` seconds; never shortens an existing hold."""
        self.db.execute(
            "INSERT INTO domains (host, next_allowed_at, backoff) VALUES (?, ?, ?) "
            "ON CONFLICT(host) DO UPDATE SET next_allowed_at = MAX(next_allowed_at, excluded.next_allowed_at), "
            "backoff = COALESCE(?, backoff)",
            (host, time.time() + delay, backoff or 0.0, backoff),
        )

    @staticmethod
    def hard_block(error: dict) -> bool:
        return error.get("backend") == "browser" and (
            error.get("category") == "blocked_by_site" or error.get("status") == 403)

    def domain_delay(self, host: str, backend: str, floor: float) -> float:
        column = {"jina": "jina_delay", "browser": "browser_delay"}[backend]
        row = self.db.execute(f"SELECT {column} FROM domains WHERE host = ?", (host,)).fetchone()
        return max(floor, row[0]) if row else floor

    def record_outcome(self, host: str, backend: str, floor: float, success: bool = False,
                       limited: bool = False, retry_after: Optional[float] = None,
                       blocked: bool = False) -> float:
        """Atomically adapt pacing and breaker state across processes.

        The adaptive delay is capped at ten minutes. An explicit longer
        Retry-After still holds the domain for its full duration.
        """
        column = {"jina": "jina_delay", "browser": "browser_delay"}[backend]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT OR IGNORE INTO domains (host) VALUES (?)", (host,))
            delay, count, broken = self.db.execute(
                f"SELECT {column}, block_count, broken_until FROM domains WHERE host = ?", (host,)
            ).fetchone()
            delay = max(floor, delay)
            if limited:
                delay = min(600.0, max(delay * 2, retry_after or 0, floor))
                # A zero-floor backend still needs a meaningful first backoff.
                if delay == 0:
                    delay = 10.0
            elif success:
                delay = max(floor, delay * 0.75)
            if success:
                count, broken = 0, 0
            elif backend == "browser":
                if blocked:
                    count += 1
                    if count >= 3:
                        broken = max(broken, time.time() + 1800)
                else:
                    count = 0  # only consecutive hard blocks count
            hold = max(delay, retry_after or 0) if limited else delay
            self.db.execute(
                f"UPDATE domains SET {column} = ?, block_count = ?, broken_until = ?, "
                "next_allowed_at = MAX(next_allowed_at, ?), backoff = ? WHERE host = ?",
                (delay, count, broken, time.time() + hold, delay if delay > floor else 0, host),
            )
            self.db.execute("COMMIT")
            return delay
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def breaker_wait(self, host: str) -> float:
        row = self.db.execute("SELECT broken_until FROM domains WHERE host = ?", (host,)).fetchone()
        return max(0.0, row[0] - time.time()) if row else 0.0

    def jina_wait(self) -> float:
        row = self.db.execute("SELECT until FROM cooldowns WHERE name = 'jina'").fetchone()
        return max(0.0, row[0] - time.time()) if row else 0.0

    def hold_jina(self, retry_after: Optional[float]) -> None:
        self.db.execute(
            "INSERT INTO cooldowns (name, until) VALUES ('jina', ?) "
            "ON CONFLICT(name) DO UPDATE SET until = MAX(until, excluded.until)",
            (time.time() + (retry_after if retry_after is not None else 60.0),),
        )

    def discipline_status(self) -> dict:
        now = time.time()
        rows = self.db.execute(
            "SELECT host, next_allowed_at, broken_until, jina_delay, browser_delay FROM domains ORDER BY host"
        ).fetchall()
        return {"domains": [
            {"host": host, "hold_seconds": round(max(0, nxt - now), 1),
             "breaker_seconds": round(max(0, broken - now), 1),
             "jina_delay_seconds": jd, "browser_delay_seconds": bd}
            for host, nxt, broken, jd, bd in rows
            if nxt > now or broken > now or jd > 0 or bd > 1
        ], "jina_cooldown_seconds": round(self.jina_wait(), 1)}
