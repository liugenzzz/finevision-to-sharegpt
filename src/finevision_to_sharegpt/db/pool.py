from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import MysqlConfig


class MySQLUnavailable(RuntimeError):
    """Raised when the driver is missing or the server cannot be reached."""


def import_driver() -> Any:
    try:
        import pymysql  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise MySQLUnavailable(
            "PyMySQL is not installed; install the 'mysql' extra to use db mode"
        ) from exc
    return pymysql


class ConnectionPool:
    """One connection per worker thread, reconnected on a dropped socket.

    Translation runs one thread per backend slot, so a shared connection would
    serialize every write. A thread-local connection keeps writes parallel
    without the bookkeeping of a checkout pool.
    """

    def __init__(self, config: MysqlConfig) -> None:
        self.config = config
        self.driver = import_driver()
        self._local = threading.local()
        self._all: list[Any] = []
        self._lock = threading.Lock()

    def _connect(self) -> Any:
        try:
            return self.driver.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset=self.config.charset,
                connect_timeout=self.config.connect_timeout,
                autocommit=True,
            )
        except Exception as exc:
            raise MySQLUnavailable(str(exc)) from exc

    def connection(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def run(self, action: Callable[[Any], Any], retries: int = 1) -> Any:
        """Run ``action`` with a cursor, reconnecting once on a dead socket.

        Internal MySQL instances close idle connections on ``wait_timeout``,
        which a long parquet scan hits routinely between flushes.
        """

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            conn = self.connection()
            try:
                with conn.cursor() as cursor:
                    return action(cursor)
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    break
                self._discard_local(conn)
                time.sleep(0.5 * (attempt + 1))
        raise MySQLUnavailable(str(last_error)) from last_error

    def _discard_local(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            if conn in self._all:
                self._all.remove(conn)
        self._local.conn = None

    def close(self) -> None:
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()


class BatchWriter:
    """Accumulates rows for one statement and flushes in ``executemany`` batches.

    A round trip per sample would dominate the run, so rows queue until the
    batch is full or the interval elapses. ``flush`` is also called from the
    pipeline's ``finally`` path so the tail batch is never lost.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        statement: str,
        batch_size: int,
        flush_interval_seconds: float,
        before_flush: Callable[[], None] | None = None,
    ) -> None:
        self.pool = pool
        self.statement = statement
        self.before_flush = before_flush
        self.batch_size = max(1, batch_size)
        self.flush_interval_seconds = flush_interval_seconds
        self._rows: list[tuple[Any, ...]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def add(self, row: tuple[Any, ...]) -> None:
        with self._lock:
            self._rows.append(row)
            due = len(self._rows) >= self.batch_size or (
                self.flush_interval_seconds > 0
                and time.monotonic() - self._last_flush >= self.flush_interval_seconds
            )
            pending = self._take() if due else []
        if pending:
            self._write(pending)

    def flush(self) -> None:
        with self._lock:
            pending = self._take()
        if pending:
            self._write(pending)

    def _take(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        self._last_flush = time.monotonic()
        return rows

    def _write(self, rows: list[tuple[Any, ...]]) -> None:
        # Status and translation writes reference rows the source writer still
        # holds, so its batch has to land first or they match nothing.
        if self.before_flush is not None:
            self.before_flush()
        self.pool.run(lambda cursor: cursor.executemany(self.statement, rows))
