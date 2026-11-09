"""Database access with connection pooling, primary circuit breaker, and
read failover to the streaming replica (resilient mode)."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import config
from .logs import log_event
from .metrics import DB_FAILOVER_READS, DEPENDENCY_UP
from .resilience import CircuitBreaker

served_from_var: ContextVar[str] = ContextVar("served_from", default="primary")


class DatabaseUnavailable(Exception):
    pass


def _make_pool(dsn: str, name: str) -> ConnectionPool:
    kwargs: dict[str, Any] = {
        "row_factory": dict_row,
        "connect_timeout": 2 if config.RESILIENT else 10,
        "application_name": f"{config.SERVICE_NAME}-{config.INSTANCE_ID}",
    }
    if config.RESILIENT:
        kwargs["options"] = f"-c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}"
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=10,
        kwargs=kwargs,
        open=False,
        name=name,
        timeout=config.DB_POOL_TIMEOUT_S,
        # resilient: validate pooled connections so broken ones (after a DB restart) are discarded
        check=ConnectionPool.check_connection if config.RESILIENT else None,
        reconnect_timeout=3600,
    )
    pool.open(wait=False)
    return pool


class Database:
    def __init__(self) -> None:
        self.primary = _make_pool(config.DB_PRIMARY_DSN, "primary")
        self.replica = _make_pool(config.DB_REPLICA_DSN, "replica") if config.DB_REPLICA_DSN else None
        self.primary_breaker = (
            CircuitBreaker("db_primary", failure_threshold=3, reset_timeout=3.0) if config.RESILIENT else None
        )
        self._probe = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe.start()

    # ---------------------------------------------------------------- writes
    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Atomic unit of work on the primary: commit on success, rollback on error."""
        if self.primary_breaker and not self.primary_breaker.allow_request():
            raise DatabaseUnavailable("primary circuit open")
        try:
            with self.primary.connection() as conn:
                yield conn
        except (psycopg.OperationalError, PoolTimeout) as exc:
            if self.primary_breaker:
                self.primary_breaker.record_failure()
            raise DatabaseUnavailable(str(exc)) from exc
        except BaseException:
            # business/validation error: the database itself was reachable
            if self.primary_breaker:
                self.primary_breaker.record_success()
            raise
        if self.primary_breaker:
            self.primary_breaker.record_success()

    @contextmanager
    def autocommit(self) -> Iterator[psycopg.Connection]:
        """Baseline write path: every statement commits on its own (no atomicity)."""
        try:
            with self.primary.connection() as conn:
                conn.autocommit = True
                try:
                    yield conn
                finally:
                    conn.autocommit = False
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DatabaseUnavailable(str(exc)) from exc

    # ----------------------------------------------------------------- reads
    def query(self, sql: str, params: tuple | dict = (), *, one: bool = False) -> Any:
        """Read query: primary first; on failure fall back to the replica (resilient)."""
        try:
            if self.primary_breaker and not self.primary_breaker.allow_request():
                raise DatabaseUnavailable("primary circuit open")
            with self.primary.connection() as conn:
                rows = conn.execute(sql, params).fetchall()
            if self.primary_breaker:
                self.primary_breaker.record_success()
            served_from_var.set("primary")
            return (rows[0] if rows else None) if one else rows
        except (psycopg.OperationalError, PoolTimeout, DatabaseUnavailable) as exc:
            if self.primary_breaker and not isinstance(exc, DatabaseUnavailable):
                self.primary_breaker.record_failure()
            if not self.replica:
                raise DatabaseUnavailable(str(exc)) from exc
        except BaseException:
            if self.primary_breaker:
                self.primary_breaker.record_success()
            raise
        try:
            with self.replica.connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DatabaseUnavailable(f"primary and replica unavailable: {exc}") from exc
        DB_FAILOVER_READS.labels(config.SERVICE_NAME).inc()
        served_from_var.set("replica")
        return (rows[0] if rows else None) if one else rows

    # ---------------------------------------------------------- health probe
    def _check(self, dsn: str) -> bool:
        try:
            with psycopg.connect(dsn, connect_timeout=1) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _probe_loop(self) -> None:
        last: dict[str, bool] = {}
        while True:
            targets = {"db_primary": config.DB_PRIMARY_DSN}
            if config.DB_REPLICA_DSN:
                targets["db_replica"] = config.DB_REPLICA_DSN
            for name, dsn in targets.items():
                ok = self._check(dsn)
                DEPENDENCY_UP.labels(config.SERVICE_NAME, name).set(1 if ok else 0)
                if last.get(name) != ok:
                    log_event("dependency_health_changed", dependency=name, up=ok)
                    last[name] = ok
            time.sleep(2)

    def status(self) -> dict[str, bool]:
        result = {"db_primary": self._check(config.DB_PRIMARY_DSN)}
        if config.DB_REPLICA_DSN:
            result["db_replica"] = self._check(config.DB_REPLICA_DSN)
        return result


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
