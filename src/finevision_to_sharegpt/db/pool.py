from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .config import MysqlConfig


class MySQLUnavailable(RuntimeError):
    """Raised when the driver is missing or the server cannot be reached."""


def _explain_connect_failure(exc: Exception, config: MysqlConfig) -> str:
    """Name the likely cause, because the driver text alone rarely does.

    "Too many connections" in particular reads like a server misconfiguration
    when it is usually stale connections left by aborted runs: MySQL holds
    each one until wait_timeout, eight hours by default.
    """

    text = str(exc)
    lowered = text.lower()
    if "too many connections" in lowered:
        return (
            f"{text}\n"
            f"  The server is at its connection limit. Check what is holding them:\n"
            f"    SHOW STATUS LIKE 'Threads_connected';\n"
            f"    SELECT command, COUNT(*), MAX(time) FROM information_schema.processlist "
            f"GROUP BY command;\n"
            f"  Many idle 'Sleep' connections mean earlier runs died before closing "
            f"theirs; MySQL keeps those until wait_timeout. Either wait it out, raise\n"
            f"    SET GLOBAL max_connections = 500;\n"
            f"  or restart mysqld. Note this pool opens one connection per worker "
            f"thread, so total backend concurrency is the number to size against."
        )
    if "access denied" in lowered:
        return f"{text}\n  Check mysql.user/password in the task config and $FV_MYSQL_PASSWORD."
    if "can't connect" in lowered or "connection refused" in lowered:
        return (
            f"{text}\n"
            f"  Nothing is listening on {config.host}:{config.port}. Start it with\n"
            f"    bash scripts/setup_local_mysql.sh /mnt/fv/mysql"
        )
    return text


def import_driver() -> Any:
    try:
        import pymysql  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise MySQLUnavailable(
            "PyMySQL is not installed; install the 'mysql' extra to use db mode"
        ) from exc
    return pymysql


class ConnectionPool:
    """A bounded pool of connections shared by all threads.

    Translation runs one worker thread per backend slot, and those threads
    spend nearly all their time waiting on the model, not on MySQL. Giving
    each one a permanent connection would size the server's connection limit
    against total concurrency for no benefit; a small shared pool serves
    hundreds of workers because every database call is short and batched.

    Connections are created lazily up to ``size``, handed out one at a time,
    and returned when the call finishes. A call that errors discards its
    connection rather than returning it, since its state is unknown.
    """

    def __init__(self, config: MysqlConfig, size: int | None = None) -> None:
        self.config = config
        self.size = max(1, size if size is not None else config.pool_size)
        self.checkout_timeout = float(config.checkout_timeout_seconds)
        self.driver = import_driver()
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

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
            raise MySQLUnavailable(_explain_connect_failure(exc, self.config)) from exc

    def acquire(self) -> Any:
        if self._closed:
            raise MySQLUnavailable("connection pool is closed")
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            room = self._created < self.size
            if room:
                self._created += 1
        if room:
            try:
                return self._connect()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            return self._idle.get(timeout=self.checkout_timeout)
        except queue.Empty as exc:
            raise MySQLUnavailable(
                f"no connection free within {self.checkout_timeout:.0f}s "
                f"(pool size {self.size}). Raise mysql.pool_size if the database "
                f"is genuinely the bottleneck, or lower backend concurrency."
            ) from exc

    def release(self, conn: Any) -> None:
        if self._closed:
            self._destroy(conn)
            return
        self._idle.put(conn)

    def _destroy(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._created = max(0, self._created - 1)

    def run(self, action: Callable[[Any], Any], retries: int = 1) -> Any:
        """Run ``action`` with a cursor, retrying once on a dead connection.

        Internal MySQL instances close idle connections on ``wait_timeout``,
        which a long parquet scan hits routinely between flushes.
        """

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            conn = self.acquire()
            try:
                with conn.cursor() as cursor:
                    result = action(cursor)
            except Exception as exc:
                last_error = exc
                # The connection may be mid-statement or dead; either way its
                # state is unknown, so it is dropped rather than reused.
                self._destroy(conn)
                if attempt >= retries:
                    break
                time.sleep(0.5 * (attempt + 1))
                continue
            self.release(conn)
            return result
        raise MySQLUnavailable(_explain_connect_failure(last_error, self.config)) from last_error

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._destroy(conn)


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
