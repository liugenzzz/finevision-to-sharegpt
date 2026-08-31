from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def parquet_num_rows(path: Path | str) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def stream_parquet_rows(path: Path | str, batch_size: int = 1024) -> Iterator[dict[str, Any]]:
    for _index, row in iter_parquet_rows_from(path, start_row=0, batch_size=batch_size):
        yield row


def iter_parquet_rows_from(
    path: Path | str,
    start_row: int = 0,
    batch_size: int = 1024,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(row_index, row)`` from ``start_row`` onwards.

    Row groups entirely below ``start_row`` are never read, so resuming a
    dataset that is already millions of rows deep skips the decode cost
    instead of streaming past it. ``row_index`` stays absolute within the
    file so sample ids keep matching earlier runs.
    """

    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata
    offset = 0
    first_group = metadata.num_row_groups
    for group in range(metadata.num_row_groups):
        rows_in_group = metadata.row_group(group).num_rows
        if offset + rows_in_group > start_row:
            first_group = group
            break
        offset += rows_in_group
    if first_group >= metadata.num_row_groups:
        return
    index = offset
    for batch in parquet_file.iter_batches(
        batch_size=batch_size,
        row_groups=list(range(first_group, metadata.num_row_groups)),
    ):
        for row in batch.to_pylist():
            if index >= start_row:
                yield index, row
            index += 1
