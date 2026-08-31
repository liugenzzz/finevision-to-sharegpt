from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator

from . import schema
from .config import MysqlConfig
from .fingerprint import source_fingerprint
from .ledger import ConsumptionLedger, DatasetVersion, ScanPlan
from .pool import BatchWriter, ConnectionPool, MySQLUnavailable

__all__ = ["MySQLLedger", "MySQLUnavailable"]

# The upserts below use the VALUES() function rather than the row-alias form
# MySQL 8.0.19 introduced. Verified on both engines: VALUES() works on MySQL
# 8.4 and MariaDB 10.11, while `INSERT ... VALUES (...) AS new` is a syntax
# error on MariaDB. MySQL 8.4 does raise warning 1287 (deprecated) for it,
# but the warning reaches neither the error log nor PyMySQL, and switching
# would need a second dialect plus different handling for the
# INSERT ... SELECT statement. Revisit when MySQL actually removes it.

_UPSERT_SOURCE = """
INSERT INTO sample_source
  (version_id, dataset, sample_id, parquet_name, row_index, conversations,
   image_paths, image_count, status, lang_assigned, reject_reason, batch_id,
   claimed_at, claim_expires_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, NOW(),
        DATE_ADD(NOW(), INTERVAL %s SECOND))
ON DUPLICATE KEY UPDATE
  parquet_name     = VALUES(parquet_name),
  row_index        = VALUES(row_index),
  conversations    = VALUES(conversations),
  image_paths      = VALUES(image_paths),
  image_count      = VALUES(image_count),
  status           = VALUES(status),
  reject_reason    = VALUES(reject_reason),
  batch_id         = VALUES(batch_id),
  claimed_at       = VALUES(claimed_at),
  claim_expires_at = VALUES(claim_expires_at)
"""

_UPDATE_STATUS = """
UPDATE sample_source
   SET status = %s,
       lang_assigned = COALESCE(%s, lang_assigned),
       reject_reason = %s,
       done_at = CASE WHEN %s = 'done' THEN NOW() ELSE done_at END,
       claim_expires_at = NULL
 WHERE version_id = %s AND sample_id = %s
"""

_INSERT_TRANSLATION = """
INSERT INTO sample_translation
  (source_id, version_id, sample_id, conversations, backend_name, model_name,
   prompt_version, batch_id, latency_ms, created_at)
SELECT s.id, s.version_id, s.sample_id, %s, %s, %s, %s, %s, %s, NOW()
  FROM sample_source s
 WHERE s.version_id = %s AND s.sample_id = %s
ON DUPLICATE KEY UPDATE
  conversations = VALUES(conversations),
  backend_name  = VALUES(backend_name),
  batch_id      = VALUES(batch_id),
  latency_ms    = VALUES(latency_ms),
  created_at    = NOW()
"""

_UPSERT_CURSOR = """
INSERT INTO dataset_cursor (version_id, parquet_name, max_scanned_row_index, fully_scanned, updated_at)
VALUES (%s, %s, %s, %s, NOW())
ON DUPLICATE KEY UPDATE
  max_scanned_row_index = GREATEST(max_scanned_row_index, VALUES(max_scanned_row_index)),
  fully_scanned = GREATEST(fully_scanned, VALUES(fully_scanned)),
  updated_at = NOW()
"""

_UNFINISHED_PREDICATE = (
    "(status IN ('pending','failed') "
    "OR (status = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at < NOW()))"
)


class MySQLLedger(ConsumptionLedger):
    """Persists sample consumption so repeated runs pull only new samples."""

    def __init__(
        self,
        config: MysqlConfig,
        completed_ids: set[str] | None = None,
        batch_id: str | None = None,
        ensure_schema: bool = True,
    ) -> None:
        self.config = config
        self.batch_id = batch_id
        self.completed_ids = completed_ids if completed_ids is not None else set()
        self.pool = ConnectionPool(config)
        if ensure_schema:
            self.ensure_schema()
        else:
            self.pool.run(lambda cursor: cursor.execute("SELECT 1"))
        self._source_writer = BatchWriter(
            self.pool, _UPSERT_SOURCE, config.batch_size, config.flush_interval_seconds
        )
        self._status_writer = BatchWriter(
            self.pool,
            _UPDATE_STATUS,
            config.batch_size,
            config.flush_interval_seconds,
            before_flush=self._source_writer.flush,
        )
        self._translation_writer = BatchWriter(
            self.pool,
            _INSERT_TRANSLATION,
            config.batch_size,
            config.flush_interval_seconds,
            before_flush=self._source_writer.flush,
        )
        self._cursor_marks: dict[tuple[int, str], int] = {}
        self._finished: set[tuple[int, str]] = set()
        self._cursor_lock = threading.Lock()

    # -- schema ---------------------------------------------------------

    def ensure_schema(self) -> None:
        def create(cursor: Any) -> None:
            for statement in schema.table_statements(self.config.collation):
                cursor.execute(statement)

        self.pool.run(create)

    def ensure_view(self, dataset: str) -> None:
        self.pool.run(lambda cursor: cursor.execute(schema.create_view(dataset)))

    # -- dataset versions -----------------------------------------------

    def open_dataset(self, dataset: str, source_path: Path, images_root: Path) -> DatasetVersion:
        fingerprint = source_fingerprint(source_path)

        def resolve(cursor: Any) -> int:
            cursor.execute(
                """
                INSERT INTO dataset_version
                  (dataset, source_file, source_hash, file_size, file_mtime, images_root, first_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                  source_file = VALUES(source_file),
                  file_size   = VALUES(file_size),
                  file_mtime  = VALUES(file_mtime),
                  images_root = VALUES(images_root)
                """,
                (
                    dataset,
                    str(source_path),
                    fingerprint.source_hash,
                    fingerprint.file_size,
                    fingerprint.file_mtime,
                    str(images_root),
                ),
            )
            cursor.execute(
                "SELECT id FROM dataset_version WHERE dataset = %s AND source_hash = %s",
                (dataset, fingerprint.source_hash),
            )
            return int(cursor.fetchone()[0])

        version_id = self.pool.run(resolve)
        self.ensure_view(dataset)
        self._recover_expired(version_id)
        return DatasetVersion(dataset=dataset, version_id=version_id, source_hash=fingerprint.source_hash)

    def _recover_expired(self, version_id: int) -> int:
        """Return crashed-mid-flight claims to the pool of pending samples."""

        def recover(cursor: Any) -> int:
            cursor.execute(
                """
                UPDATE sample_source
                   SET status = 'pending', batch_id = NULL, claimed_at = NULL, claim_expires_at = NULL
                 WHERE version_id = %s
                   AND status = 'claimed'
                   AND claim_expires_at IS NOT NULL
                   AND claim_expires_at < NOW()
                """,
                (version_id,),
            )
            return int(cursor.rowcount or 0)

        return self.pool.run(recover)

    # -- scanning --------------------------------------------------------

    def scan_plan(
        self, version: DatasetVersion, parquet_name: str, for_ingest: bool = False
    ) -> ScanPlan:
        """Where to resume scanning one parquet.

        ``for_ingest`` is the bulk-load case (``db-scan``): every row below the
        watermark is already in the table whatever its status, so the scan
        resumes at the watermark and re-reads nothing. The consume path cannot
        do that — a ``pending`` row below the watermark is exactly what it is
        looking for — so it drops back to the earliest unfinished row instead.
        """

        version_id = version.version_id
        if version_id is None:
            return ScanPlan()

        def query(cursor: Any) -> ScanPlan:
            cursor.execute(
                "SELECT max_scanned_row_index FROM dataset_cursor WHERE version_id = %s AND parquet_name = %s",
                (version_id, parquet_name),
            )
            row = cursor.fetchone()
            watermark = int(row[0]) + 1 if row is not None else 0
            if for_ingest:
                return ScanPlan(start_row=watermark, gap_end=watermark)
            cursor.execute(
                "SELECT MIN(row_index) FROM sample_source "
                f"WHERE version_id = %s AND parquet_name = %s AND {_UNFINISHED_PREDICATE}",
                (version_id, parquet_name),
            )
            found = cursor.fetchone()
            unfinished = int(found[0]) if found is not None and found[0] is not None else None
            start = watermark if unfinished is None else min(unfinished, watermark)
            if start >= watermark:
                return ScanPlan(start_row=start, gap_end=watermark)
            cursor.execute(
                "SELECT sample_id FROM sample_source "
                "WHERE version_id = %s AND parquet_name = %s AND row_index >= %s AND row_index < %s "
                "AND (status IN ('done','rejected') "
                "     OR (status = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at >= NOW()))",
                (version_id, parquet_name, start, watermark),
            )
            consumed = {str(item[0]) for item in cursor.fetchall()}
            return ScanPlan(start_row=start, gap_end=watermark, consumed_ids=consumed)

        return self.pool.run(query)

    def is_consumed(self, version: DatasetVersion, sample_id: str, row_index: int, plan: ScanPlan) -> bool:
        if row_index >= plan.gap_end:
            return False
        return sample_id in plan.consumed_ids

    # -- writes ----------------------------------------------------------

    def claim(
        self,
        version: DatasetVersion,
        parquet_name: str,
        row_index: int,
        sample_id: str,
        conversations: list[dict[str, str]],
        image_paths: list[str],
        status: str = "claimed",
    ) -> None:
        if version.version_id is None:
            return
        self._source_writer.add(
            (
                version.version_id,
                version.dataset,
                sample_id,
                parquet_name,
                row_index,
                json.dumps(conversations, ensure_ascii=False),
                json.dumps(image_paths, ensure_ascii=False),
                len(image_paths),
                status,
                None,
                self.batch_id,
                self.config.claim_ttl_seconds,
            )
        )

    def mark_rejected(
        self,
        version: DatasetVersion,
        parquet_name: str,
        row_index: int,
        sample_id: str,
        reason: str | None,
    ) -> None:
        if version.version_id is None:
            return
        self._source_writer.add(
            (
                version.version_id,
                version.dataset,
                sample_id,
                parquet_name,
                row_index,
                None,
                None,
                0,
                "rejected",
                (reason or "")[:255] or None,
                self.batch_id,
                self.config.claim_ttl_seconds,
            )
        )

    def mark_done(self, version: DatasetVersion, sample_id: str, lang: str) -> None:
        self.completed_ids.add(sample_id)
        if version.version_id is None:
            return
        self._status_writer.add(("done", lang, None, "done", version.version_id, sample_id))

    def mark_failed(self, version: DatasetVersion, sample_id: str, error: str | None) -> None:
        if version.version_id is None:
            return
        self._status_writer.add(
            ("failed", None, (error or "")[:255] or None, "failed", version.version_id, sample_id)
        )

    def record_translation(
        self,
        version: DatasetVersion,
        sample_id: str,
        conversations: list[dict[str, str]],
        backend_name: str | None,
        model_name: str,
        prompt_version: str,
        latency_ms: int | None,
    ) -> None:
        if version.version_id is None:
            return
        self._translation_writer.add(
            (
                json.dumps(conversations, ensure_ascii=False),
                backend_name,
                model_name,
                prompt_version,
                self.batch_id,
                latency_ms,
                version.version_id,
                sample_id,
            )
        )

    def note_scanned(self, version: DatasetVersion, parquet_name: str, row_index: int) -> None:
        if version.version_id is None:
            return
        key = (version.version_id, parquet_name)
        with self._cursor_lock:
            if row_index > self._cursor_marks.get(key, -1):
                self._cursor_marks[key] = row_index

    def finish_parquet(self, version: DatasetVersion, parquet_name: str) -> None:
        if version.version_id is None:
            return
        with self._cursor_lock:
            self._finished.add((version.version_id, parquet_name))
        self.flush()

    # -- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        # The cursor goes last: a watermark must never cover rows whose own
        # insert has not landed, or a crash here would skip them forever.
        self._source_writer.flush()
        self._status_writer.flush()
        self._translation_writer.flush()
        self._flush_cursor()

    def _flush_cursor(self) -> None:
        with self._cursor_lock:
            marks = list(self._cursor_marks.items())
            finished = set(self._finished)
        if not marks:
            return
        rows = [
            (version_id, parquet_name, row_index, 1 if (version_id, parquet_name) in finished else 0)
            for (version_id, parquet_name), row_index in marks
        ]
        self.pool.run(lambda cursor: cursor.executemany(_UPSERT_CURSOR, rows))

    def close(self) -> None:
        self.pool.close()

    # -- reporting --------------------------------------------------------

    def status_counts(self, dataset: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE s.dataset = %s" if dataset else ""
        params = (dataset,) if dataset else ()

        def query(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(
                "SELECT s.dataset, v.source_hash, s.status, COUNT(*) "
                "FROM sample_source s JOIN dataset_version v ON v.id = s.version_id "
                f"{where} GROUP BY s.dataset, v.source_hash, s.status "
                "ORDER BY s.dataset, v.source_hash, s.status",
                params,
            )
            return [
                {"dataset": row[0], "source_hash": row[1], "status": row[2], "count": int(row[3])}
                for row in cursor.fetchall()
            ]

        return self.pool.run(query)

    def storage_report(self) -> dict[str, Any]:
        """Row counts and on-disk size per table.

        Ingesting a multi-terabyte tree makes the database itself a capacity
        question, so the numbers to extrapolate from should be one command
        away rather than a hand-written information_schema query.
        """

        def query(cursor: Any) -> dict[str, Any]:
            cursor.execute(
                "SELECT table_name, table_rows, "
                "       ROUND((data_length + index_length) / 1024 / 1024, 1) AS mb "
                "  FROM information_schema.tables "
                " WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                " ORDER BY (data_length + index_length) DESC"
            )
            tables = [
                {"table": row[0], "approx_rows": int(row[1] or 0), "size_mb": float(row[2] or 0)}
                for row in cursor.fetchall()
            ]
            cursor.execute("SELECT COUNT(*) FROM sample_source")
            exact = int(cursor.fetchone()[0])
            total_mb = round(sum(item["size_mb"] for item in tables), 1)
            report: dict[str, Any] = {
                "tables": tables,
                "sample_source_rows": exact,
                "total_mb": total_mb,
                "bytes_per_row": round(total_mb * 1024 * 1024 / exact, 1) if exact else 0,
            }
            if exact < 10000:
                # Page and index overhead dominates a small table, so the
                # per-row figure only becomes projectable once the load is
                # large enough to amortise it.
                report["note"] = (
                    f"only {exact} rows: bytes_per_row is dominated by page overhead. "
                    "Scan at least ~100k rows before projecting a full load."
                )
            return report

        return self.pool.run(query)

    def iter_export_records(
        self,
        dataset: str | None = None,
        batch_id: str | None = None,
        lang: str | None = None,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Stream done samples as ShareGPT records, newest translation wins.

        Keyset pagination keeps memory flat without a server-side cursor.
        """

        filters = ["s.status = 'done'"]
        params: list[Any] = []
        if dataset:
            filters.append("s.dataset = %s")
            params.append(dataset)
        if batch_id:
            filters.append("s.batch_id = %s")
            params.append(batch_id)
        if lang:
            filters.append("s.lang_assigned = %s")
            params.append(lang)
        where = " AND ".join(filters)
        last_id = 0
        while True:
            def query(cursor: Any, last_id: int = last_id) -> list[tuple[Any, ...]]:
                cursor.execute(
                    "SELECT s.id, s.sample_id, s.image_paths, s.conversations, s.lang_assigned, "
                    "       (SELECT t.conversations FROM sample_translation t "
                    "         WHERE t.source_id = s.id ORDER BY t.created_at DESC, t.id DESC LIMIT 1) "
                    f"FROM sample_source s WHERE {where} AND s.id > %s "
                    "ORDER BY s.id LIMIT %s",
                    (*params, last_id, page_size),
                )
                return list(cursor.fetchall())

            rows = self.pool.run(query)
            if not rows:
                return
            for row in rows:
                last_id = int(row[0])
                translated = _load_json(row[5])
                conversations = translated if row[4] == "zh" and translated else _load_json(row[3])
                if not conversations:
                    continue
                yield {
                    "id": row[1],
                    "images": _load_json(row[2]) or [],
                    "conversations": conversations,
                }


def _load_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
