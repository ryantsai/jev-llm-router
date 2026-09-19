"""Bounded persistent affinity: only hashed references and routing decisions, never prompts."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Decision:
    tier: str
    model: str
    effort: str
    reason: str


class RouteError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_route"):
        super().__init__(message)
        self.status = status
        self.code = code


class AffinityStore:
    def __init__(self, path: str, ttl: int = 86400, limit: int = 10000,
                 clock: Callable[[], float] = time.time):
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS affinity (
            key TEXT PRIMARY KEY, tier TEXT NOT NULL, model TEXT NOT NULL,
            effort TEXT NOT NULL, expires REAL NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS expiry ON affinity(expires)")
        self.lock = threading.RLock()
        self.ttl, self.limit, self.clock = ttl, limit, clock

    @staticmethod
    def digest(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def resolve(self, keys: list[str]) -> tuple[Decision | None, list[str]]:
        with self.lock, self.db:
            now = self.clock()
            self.db.execute("DELETE FROM affinity WHERE expires <= ?", (now,))
            found: Decision | None = None
            missing = []
            for key in dict.fromkeys(keys):
                row = self.db.execute(
                    "SELECT tier, model, effort FROM affinity WHERE key = ?",
                    (self.digest(key),),
                ).fetchone()
                if row is None:
                    missing.append(key)
                    continue
                current = Decision(*row, reason="affinity")
                if found and (found.model, found.effort) != (current.model, current.effort):
                    raise RouteError("Input combines incompatible routing histories; start a fresh task.",
                                     409, "affinity_conflict")
                found = current
            return found, missing

    def bind(self, keys: list[str], decision: Decision) -> Decision:
        """Atomically claim references; never overwrite an existing, incompatible chain."""
        with self.lock, self.db:
            existing, _ = self.resolve(keys)
            if existing and (existing.model, existing.effort) != (decision.model, decision.effort):
                raise RouteError("Concurrent requests disagree on the session route.",
                                 409, "affinity_conflict")
            now = self.clock()
            self.db.executemany(
                "INSERT OR REPLACE INTO affinity VALUES (?, ?, ?, ?, ?)",
                [(self.digest(key), decision.tier, decision.model, decision.effort, now + self.ttl)
                 for key in dict.fromkeys(keys)],
            )
            self.db.execute("""DELETE FROM affinity WHERE key IN (
                SELECT key FROM affinity ORDER BY expires DESC LIMIT -1 OFFSET ?)
                """, (self.limit,))
        return replace(decision, reason="affinity") if existing else decision

    def close(self) -> None:
        with self.lock:
            self.db.close()
