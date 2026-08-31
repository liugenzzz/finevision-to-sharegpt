#!/usr/bin/env python3
"""Estimate the temp-dir and memory a dataset tree needs before running it.

Directory datasets (FineVision style: one folder of parquet per dataset) are
read in place, so they need no temp space and no upfront decompression.

Zip datasets are different: ``extract_parquets_from_zip`` expands every
parquet member of one zip into ``TMPDIR`` at once and reads each member whole
before writing it, so a run needs temp space for the largest zip's full
uncompressed size and RAM for its largest single member. Datasets are
processed one at a time, so the requirement is the largest zip, not the sum.

Usage:
    python scripts/estimate_space.py /mnt/fv/data/zips
    python scripts/estimate_space.py /mnt/.../FineVision
"""

from __future__ import annotations

import os
import shutil
import sys
import zipfile
from pathlib import Path

GB = 1024**3


def _report_zip(path: Path) -> tuple[float, float]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
    except zipfile.BadZipFile as exc:
        print(f"{path.name}: unreadable ({exc})")
        return 0.0, 0.0
    uncompressed = sum(item.file_size for item in members) / GB
    largest = max((item.file_size for item in members), default=0) / GB
    print(f"{path.name}  [zip]")
    print(f"  parquet members : {len(members)}")
    print(f"  uncompressed    : {uncompressed:8.2f} GB  -> needs temp space")
    print(f"  largest member  : {largest:8.2f} GB  -> needs memory")
    return uncompressed, largest


def _report_dir(path: Path) -> float:
    files = [item for item in sorted(path.rglob("*.parquet")) if item.is_file()]
    total = sum(item.stat().st_size for item in files) / GB
    print(f"{path.name}  [dir]")
    print(f"  parquet files   : {len(files)}")
    print(f"  on-disk size    : {total:8.2f} GB  -> read in place, no temp space")
    return total


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    if not root.exists():
        print(f"no such path: {root}")
        return 1

    if root.is_file():
        zips, dirs = [root], []
    else:
        zips = sorted(root.glob("*.zip"))
        dirs = [
            child
            for child in sorted(root.iterdir())
            if child.is_dir() and next(child.rglob("*.parquet"), None) is not None
        ]
    if not zips and not dirs:
        print(f"found no zip archives and no parquet directories under {root}")
        return 1

    temp_needed = 0.0
    memory_needed = 0.0
    for path in zips:
        uncompressed, largest = _report_zip(path)
        temp_needed = max(temp_needed, uncompressed)
        memory_needed = max(memory_needed, largest)
    for path in dirs:
        _report_dir(path)

    print()
    print(f"datasets: {len(zips)} zip, {len(dirs)} directory")
    if not zips:
        print("all datasets are directories -> TMPDIR is not used by the pipeline")
        return 0

    print(f"TMPDIR needs at least : {temp_needed:8.2f} GB")
    print(f"free memory at least  : {memory_needed:8.2f} GB")
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    if not tmpdir.exists():
        print(f"\nTMPDIR={tmpdir} does not exist yet")
        return 0
    free = shutil.disk_usage(tmpdir).free / GB
    if free < temp_needed:
        print(f"\nTMPDIR={tmpdir} has {free:.2f} GB free -> TOO SMALL")
        print("  point TMPDIR at a bigger writable volume before running")
        return 1
    print(f"\nTMPDIR={tmpdir} has {free:.2f} GB free -> OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
