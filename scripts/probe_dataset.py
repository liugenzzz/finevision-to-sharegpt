#!/usr/bin/env python3
"""Open one sample of a collection and say whether the pipeline can read it.

``survey_roots.py`` counts file extensions, which is only enough to spot a
directory of parquet. It cannot tell a parquet holding image *bytes* from one
holding image *paths*, nor a zip of parquet shards from a zip of loose jpegs —
both look registrable from the outside. This opens the data and runs the real
row parser over the first few rows, so the verdict here is the one an actual
ingest would reach.

The judgement itself lives in ``finevision_to_sharegpt.dataset_probe`` because
the registry applies the very same check when it decides what to register: two
implementations of "can we read this" would drift, and the whole point is that
they agree.

    python scripts/probe_dataset.py /mnt/.../mm_general/OmniScience
    python scripts/probe_dataset.py /mnt/.../mm_general --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.dataset_probe import (  # noqa: E402
    VERDICTS,
    Probe,
    probe_dataset,
    probe_parquet,
    probe_zip,
)

__all__ = ["VERDICTS", "Probe", "probe_dataset", "probe_parquet", "probe_zip", "report", "main"]


def report(name: str, probe: Probe) -> None:
    print(f"  {name:<45} {probe.label}   {probe.detail or probe.source}")
    if probe.columns:
        shown = ", ".join(probe.columns[:8])
        extra = f", …(+{len(probe.columns) - 8})" if len(probe.columns) > 8 else ""
        print(f"      列: {shown}{extra}")
    if probe.rows_ok:
        print(f"      前 {probe.rows_seen} 行解析出 {probe.rows_ok} 条，每条 {probe.images} 张图")
    elif probe.rows_seen:
        # "每条 0 张图" on a missing_text verdict reads as "no images either",
        # which is the opposite of what that verdict means.
        print(f"      前 {probe.rows_seen} 行一条都没解析出来")
    if probe.sample_text:
        print(f"      示例: {probe.sample_text}")
    print(f"      → {probe.advice}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="probe whether a collection is readable")
    parser.add_argument("path", help="one collection directory, or a root of them with --all")
    parser.add_argument("--all", action="store_true", help="probe every subdirectory of path")
    parser.add_argument("--rows", type=int, default=5, help="how many rows to parse per collection")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    targets = [item for item in sorted(root.iterdir()) if item.is_dir()] if args.all else [root]
    results: list[tuple[str, Probe]] = []
    for target in targets:
        try:
            probe = probe_dataset(target, args.rows)
        except Exception as exc:  # noqa: BLE001 - one bad collection must not stop the survey
            probe = Probe("unreadable", detail=f"打开失败: {exc}")
        results.append((target.name, probe))
        report(target.name, probe)

    registrable = [name for name, probe in results if probe.verdict == "ok"]
    print(f"\n可注册 {len(registrable)} / {len(results)}: {', '.join(registrable) or '(无)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
