from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MysqlConfig


@dataclass(frozen=True)
class DatasetVersion:
    """Identifies one revision of one dataset.

    ``version_id`` is ``None`` in file mode, where no rows are persisted.
    """

    dataset: str
    version_id: int | None = None
    source_hash: str | None = None


@dataclass(frozen=True)
class ScanPlan:
    """Where to resume scanning one parquet, and what to skip on the way.

    ``start_row`` lets the reader jump whole row groups. ``consumed_ids``
    only covers the retry gap ``[start_row, gap_end)`` — rows already
    finished that sit below the watermark because an earlier row still needs
    retrying. Above ``gap_end`` nothing has been seen, so no lookup is needed.
    """

    start_row: int = 0
    gap_end: int = 0
    consumed_ids: set[str] = field(default_factory=set)


class ConsumptionLedger(ABC):
    """Tracks which samples have already been consumed.

    Two implementations back this: :class:`JsonlLedger` reproduces the
    original file-only behaviour, and ``MySQLLedger`` persists across runs and
    output directories.
    """

    @abstractmethod
    def open_dataset(self, dataset: str, zip_path: Path, images_root: Path) -> DatasetVersion: ...

    @abstractmethod
    def scan_plan(self, version: DatasetVersion, parquet_name: str) -> ScanPlan: ...

    @abstractmethod
    def is_consumed(self, version: DatasetVersion, sample_id: str, row_index: int, plan: ScanPlan) -> bool: ...

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
        return None

    def mark_rejected(
        self,
        version: DatasetVersion,
        parquet_name: str,
        row_index: int,
        sample_id: str,
        reason: str | None,
    ) -> None:
        return None

    @abstractmethod
    def mark_done(self, version: DatasetVersion, sample_id: str, lang: str) -> None: ...

    def mark_failed(self, version: DatasetVersion, sample_id: str, error: str | None) -> None:
        return None

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
        return None

    def note_scanned(self, version: DatasetVersion, parquet_name: str, row_index: int) -> None:
        return None

    def finish_parquet(self, version: DatasetVersion, parquet_name: str) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "ConsumptionLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self.flush()
        finally:
            self.close()


class JsonlLedger(ConsumptionLedger):
    """File-mode ledger: an in-memory id set, exactly as before MySQL existed."""

    def __init__(self, completed_ids: set[str] | None = None) -> None:
        self.completed_ids: set[str] = completed_ids if completed_ids is not None else set()

    def open_dataset(self, dataset: str, zip_path: Path, images_root: Path) -> DatasetVersion:
        return DatasetVersion(dataset=dataset)

    def scan_plan(self, version: DatasetVersion, parquet_name: str) -> ScanPlan:
        return ScanPlan()

    def is_consumed(self, version: DatasetVersion, sample_id: str, row_index: int, plan: ScanPlan) -> bool:
        return sample_id in self.completed_ids

    def mark_done(self, version: DatasetVersion, sample_id: str, lang: str) -> None:
        self.completed_ids.add(sample_id)


def open_ledger(
    mysql_config: MysqlConfig | None,
    completed_ids: set[str] | None = None,
    batch_id: str | None = None,
) -> ConsumptionLedger:
    """Pick a ledger. Falls back to file mode when MySQL is absent or refuses.

    ``on_connect_error='fail'`` turns a connection problem into an error
    instead, so a run never silently stops recording.
    """

    if mysql_config is None:
        return JsonlLedger(completed_ids)
    from .mysql_ledger import MySQLLedger, MySQLUnavailable

    try:
        return MySQLLedger(mysql_config, completed_ids=completed_ids, batch_id=batch_id)
    except MySQLUnavailable as exc:
        if mysql_config.fail_fast:
            raise
        print(f"[warn] mysql unavailable ({exc}); falling back to file mode")
        return JsonlLedger(completed_ids)
