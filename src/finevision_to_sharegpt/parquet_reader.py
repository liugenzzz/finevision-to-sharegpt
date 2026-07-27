from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def parquet_num_rows(path: Path | str) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def stream_parquet_rows(path: Path | str, batch_size: int = 1024) -> Iterator[dict[str, Any]]:
    parquet_file = pq.ParquetFile(path)
    for batch in parquet_file.iter_batches(batch_size=batch_size):
        for row in batch.to_pylist():
            yield row
