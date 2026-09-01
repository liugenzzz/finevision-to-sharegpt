#!/usr/bin/env python3
"""Survey a directory of collections: what format is each one, how big.

Before registering anything it is worth knowing which collections this tool
can even read. It handles directories of parquet and zip archives; a
collection stored as json, arrow or loose images needs converting first.

    python scripts/survey_roots.py /mnt/.../mm_general
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

GB = 1024**3
READABLE = {".parquet", ".zip"}


def survey(directory: Path, depth: int = 3) -> dict:
    kinds: Counter[str] = Counter()
    size = 0
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        try:
            relative_depth = len(item.relative_to(directory).parts)
        except ValueError:
            continue
        if relative_depth > depth:
            continue
        kinds[item.suffix.lower() or "(no ext)"] += 1
        try:
            size += item.stat().st_size
        except OSError:
            pass
    return {"kinds": kinds, "size": size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="survey collections under a directory")
    parser.add_argument("root")
    parser.add_argument("--depth", type=int, default=3, help="how deep to look for files")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 1

    usable, needs_work = [], []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        result = survey(child, args.depth)
        kinds = result["kinds"]
        top = ", ".join(f"{ext}×{count}" for ext, count in kinds.most_common(4)) or "(empty)"
        line = f"  {child.name:<45} {result['size'] / GB:8.1f} GB   {top}"
        (usable if READABLE & set(kinds) else needs_work).append(line)

    print(f"可直接注册（含 parquet 或 zip）: {len(usable)}")
    for line in usable:
        print(line)
    print(f"\n需要先转换格式: {len(needs_work)}")
    for line in needs_work:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
