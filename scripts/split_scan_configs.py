#!/usr/bin/env python3
"""Split one scan config into N shards that can run concurrently.

A full ingest is bytes-bound and largely I/O-bound, so several processes over
disjoint dataset sets finish far sooner than one. Sharding by dataset needs no
coordination: two processes never touch the same rows, and the ledger's claim
mechanism already tolerates concurrent writers.

Usage:
    python scripts/split_scan_configs.py configs/db_scan.json 6 -o configs/shards
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.dataset_registry import (  # noqa: E402
    load_dataset_registry,
    resolve_dataset_selection,
)

GB = 1024**3


def dataset_size(dataset) -> int:
    if dataset.is_directory:
        return sum(item.stat().st_size for item in dataset.source_path.rglob("*.parquet") if item.is_file())
    return dataset.source_path.stat().st_size if dataset.source_path.exists() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="split a scan config into N concurrent shards")
    parser.add_argument("config", help="base task config, including its mysql section")
    parser.add_argument("shards", type=int, help="how many concurrent processes to prepare for")
    parser.add_argument("-o", "--output-dir", default="configs/shards")
    args = parser.parse_args(argv)
    if args.shards < 1:
        print("shards must be at least 1", file=sys.stderr)
        return 2

    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    config = load_zip_task_config(args.config)
    registry = load_dataset_registry(config.dataset_registry)
    datasets = resolve_dataset_selection(registry, [item.name for item in config.datasets])

    sized = sorted(((dataset_size(item), item.name) for item in datasets), reverse=True)
    # Longest-processing-time first: repeatedly hand the biggest dataset to the
    # shard that currently holds the least, which keeps the finish times close.
    buckets: list[tuple[int, list[str]]] = [(0, []) for _ in range(args.shards)]
    for size, name in sized:
        index = min(range(len(buckets)), key=lambda i: buckets[i][0])
        total, names = buckets[index]
        buckets[index] = (total + size, [*names, name])

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for index, (total, names) in enumerate(buckets):
        if not names:
            continue
        shard = dict(base)
        shard["datasets"] = sorted(names)
        out = output_dir / f"shard_{index:02d}.json"
        out.write_text(json.dumps(shard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(out)
        print(f"  {out}  {len(names):4d} datasets  {total / GB:8.1f} GB")

    grand_total = sum(total for total, _ in buckets)
    spread = max(t for t, _ in buckets) - min(t for t, names in buckets if names)
    print(f"\n{len(written)} shards, {grand_total / GB:.1f} GB total, largest-smallest gap {spread / GB:.1f} GB")
    print("\nRun them together (each writes to the same database):\n")
    for out in written:
        print(
            f"  nohup env FV_MYSQL_PASSWORD=\"$FV_MYSQL_PASSWORD\" python -m finevision_to_sharegpt "
            f"db-scan --config {out} --no-images > {out.with_suffix('.log')} 2>&1 &"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
