from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
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
    skip: Callable[[int], bool] | None = None,
) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """Yield ``(row_index, row)`` from ``start_row`` onwards.

    Row groups entirely below ``start_row`` are never read, so resuming a
    dataset that is already millions of rows deep skips the decode cost
    instead of streaming past it. ``row_index`` stays absolute within the
    file so sample ids keep matching earlier runs.

    ``skip`` answers, **from the row index alone**, whether the caller is going
    to throw the row away. Sample ids are positional
    (``<dataset>:<parquet>:<row_index>``), so续跑 never needs a row's content to
    know it is already done — and decoding one costs about 4000x more than the
    index check, because it materializes the image bytes. A row group or batch
    that is skippable end to end is therefore never read at all; its rows come
    back as ``(index, None)`` so the caller still counts them and still advances
    its watermark. Without this, a restart re-decodes every finished row at
    ~7000 rows/s just to discard it.
    """

    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata
    bounds: list[tuple[int, int, int]] = []
    offset = 0
    for group in range(metadata.num_row_groups):
        rows_in_group = metadata.row_group(group).num_rows
        bounds.append((group, offset, offset + rows_in_group))
        offset += rows_in_group

    def all_skipped(start: int, end: int) -> bool:
        return skip is not None and all(skip(index) for index in range(start, end))

    for group, group_start, group_end in bounds:
        if group_end <= start_row:
            continue
        wanted_start = max(group_start, start_row)
        # 整组都要丢：连读都不用读，省掉的是磁盘 I/O 和 Arrow 解码两笔。
        if all_skipped(wanted_start, group_end):
            for index in range(wanted_start, group_end):
                yield index, None
            continue
        index = group_start
        for batch in parquet_file.iter_batches(batch_size=batch_size, row_groups=[group]):
            batch_end = index + batch.num_rows
            # 组里只有一部分要丢时，逐批再判一次：to_pylist 才是最贵的一步。
            if all_skipped(max(index, start_row), batch_end):
                for skipped_index in range(max(index, start_row), batch_end):
                    yield skipped_index, None
                index = batch_end
                continue
            for row in batch.to_pylist():
                if index >= start_row:
                    yield index, None if (skip is not None and skip(index)) else row
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
