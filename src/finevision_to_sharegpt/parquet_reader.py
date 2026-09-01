from __future__ import annotations

from collections.abc import Iterator, Sequence
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


def iter_parquet_rows_at(
    path: Path | str,
    row_indexes: Sequence[int],
    batch_size: int = 1024,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield only the requested absolute row indexes.

    Only the row groups that actually contain a wanted index are decoded, so
    picking a scattered sample costs a fraction of a full pass when the
    sampling rate is low. Indexes are yielded in ascending order regardless of
    the order they were supplied in.
    """

    wanted = sorted({int(index) for index in row_indexes if index >= 0})
    if not wanted:
        return
    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata

    bounds: list[tuple[int, int, int]] = []
    offset = 0
    for group in range(metadata.num_row_groups):
        rows_in_group = metadata.row_group(group).num_rows
        bounds.append((group, offset, offset + rows_in_group))
        offset += rows_in_group

    position = 0
    for group, start, end in bounds:
        if position >= len(wanted):
            break
        if wanted[position] >= end:
            continue
        group_wanted = []
        while position < len(wanted) and wanted[position] < end:
            group_wanted.append(wanted[position])
            position += 1
        if not group_wanted:
            continue
        targets = set(group_wanted)
        index = start
        for batch in parquet_file.iter_batches(batch_size=batch_size, row_groups=[group]):
            if index > group_wanted[-1]:
                break
            rows = batch.to_pylist()
            for row in rows:
                if index in targets:
                    yield index, row
                index += 1
