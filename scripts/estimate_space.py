#!/usr/bin/env python3
"""Estimate the temp-dir and memory a zip dataset needs before running it.

``extract_parquets_from_zip`` expands every parquet member of one zip into
``TMPDIR`` at once, and reads each member whole before writing it, so a run
needs temp space for the largest zip's full uncompressed size and RAM for
its largest single member. Datasets are processed one at a time, so the
temp requirement is the largest zip, not their sum.

Usage:
    python scripts/estimate_space.py /mnt/fv/data/zips
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

GB = 1024**3


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    archives = sorted(root.glob("*.zip")) if root.is_dir() else [root]
    if not archives:
        print(f"no zip files under {root}")
        return 1

    temp_needed = 0.0
    memory_needed = 0.0
    for path in archives:
        try:
            with zipfile.ZipFile(path) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
        except zipfile.BadZipFile as exc:
            print(f"{path.name}: unreadable ({exc})")
            continue
        uncompressed = sum(item.file_size for item in members) / GB
        largest = max((item.file_size for item in members), default=0) / GB
        temp_needed = max(temp_needed, uncompressed)
        memory_needed = max(memory_needed, largest)
        print(f"{path.name}")
        print(f"  members     : {len(members)}")
        print(f"  uncompressed: {uncompressed:8.2f} GB")
        print(f"  largest one : {largest:8.2f} GB")

    print()
    print(f"TMPDIR needs at least : {temp_needed:8.2f} GB")
    print(f"free memory at least  : {memory_needed:8.2f} GB")

    import os

    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    if tmpdir.exists():
        free = shutil.disk_usage(tmpdir).free / GB
        verdict = "OK" if free >= temp_needed else "TOO SMALL"
        print(f"\nTMPDIR={tmpdir} has {free:.2f} GB free -> {verdict}")
        if free < temp_needed:
            print("  point TMPDIR at a bigger writable volume before running")
            return 1
    else:
        print(f"\nTMPDIR={tmpdir} does not exist yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
