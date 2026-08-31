#!/usr/bin/env python3
"""Scan a data root and write an explicit dataset registry.

``auto_discover`` in the registry does the same scan at run time. Writing the
list out instead pins it: a directory appearing or disappearing later cannot
silently change what a run covers, and the file can be hand-edited to drop
datasets you do not want.

Usage:
    python scripts/register_datasets.py /mnt/.../FineVision -o configs/datasets.json
    python scripts/register_datasets.py /mnt/.../FineVision --exclude docvqa --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.dataset_registry import discover_datasets  # noqa: E402

GB = 1024**3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="write an explicit dataset registry")
    parser.add_argument("data_root", help="directory holding one subdirectory per dataset")
    parser.add_argument("-o", "--output", default="configs/datasets.json")
    parser.add_argument("--include", nargs="*", help="only these dataset directory names")
    parser.add_argument("--exclude", nargs="*", help="skip these dataset directory names")
    parser.add_argument("--min-files", type=int, default=1, help="skip datasets with fewer parquet files")
    parser.add_argument("--relative", action="store_true", help="store paths relative to data_root")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = Path(args.data_root).resolve()

    options: dict[str, list[str]] = {}
    if args.include:
        options["include"] = args.include
    if args.exclude:
        options["exclude"] = args.exclude
    discovered = discover_datasets(data_root, options or True)

    entries: dict[str, dict[str, str]] = {}
    skipped: list[str] = []
    total_files = 0
    total_size = 0.0
    for name in sorted(discovered):
        dataset = discovered[name]
        files = [item for item in dataset.source_path.rglob("*.parquet") if item.is_file()]
        if len(files) < args.min_files:
            skipped.append(f"{name} ({len(files)} files)")
            continue
        size = sum(item.stat().st_size for item in files)
        total_files += len(files)
        total_size += size
        value = (
            dataset.source_path.relative_to(data_root).as_posix()
            if args.relative
            else str(dataset.source_path)
        )
        entries[name] = {"dir": value}
        print(f"  {name:40s} {len(files):5d} parquet  {size / GB:8.2f} GB")

    if not entries:
        print(f"\nnothing to register under {data_root}", file=sys.stderr)
        return 1

    registry = {"data_root": str(data_root), "datasets": entries}
    payload = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"

    print(f"\n{len(entries)} datasets, {total_files} parquet files, {total_size / GB:.2f} GB")
    if skipped:
        print(f"skipped {len(skipped)}: {', '.join(skipped[:10])}")
    if args.dry_run:
        print("\n--dry-run, nothing written. Registry would be:\n")
        print(payload if len(payload) < 4000 else payload[:4000] + "  ... (truncated)\n")
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
