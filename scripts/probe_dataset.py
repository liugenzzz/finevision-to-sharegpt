#!/usr/bin/env python3
"""Open one sample of a collection and say whether the pipeline can read it.

``survey_roots.py`` counts file extensions, which is only enough to spot a
directory of parquet. It cannot tell a parquet holding image *bytes* from one
holding image *paths*, nor a zip of parquet shards from a zip of loose jpegs —
both look registrable from the outside. This opens the data and runs the real
row parser over the first few rows, so the verdict here is the one an actual
translation run would reach.

    python scripts/probe_dataset.py /mnt/.../mm_general/OmniScience
    python scripts/probe_dataset.py /mnt/.../mm_general --all
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.parquet_reader import iter_parquet_rows_from  # noqa: E402
from finevision_to_sharegpt.sample_parser import parse_row  # noqa: E402

VERDICTS = {
    "ok": ("✅ 可注册", "直接写进 datasets.json"),
    "missing_image": ("⚠️ 缺图", "有文本没图片字节（多半只存了图片路径），要先把图片合进 parquet"),
    "missing_text": ("⚠️ 缺文本", "有图片没对话字段，要先补 conversations/texts 再转"),
    "empty": ("⚠️ 空表", "parquet 里没有行"),
    "unreadable": ("❌ 读不了", "没有 parquet，本工具读不了，要先转格式或跳过"),
}


@dataclass
class Probe:
    """What one collection looks like to the parser the pipeline actually uses."""

    verdict: str
    detail: str = ""
    source: str = ""
    columns: list[str] = field(default_factory=list)
    rows_ok: int = 0
    rows_seen: int = 0
    images: int = 0
    sample_text: str = ""

    @property
    def label(self) -> str:
        return VERDICTS[self.verdict][0]

    @property
    def advice(self) -> str:
        return VERDICTS[self.verdict][1]


def probe_parquet(path: Path, rows: int = 5) -> Probe:
    """Parse the first ``rows`` rows of one parquet shard."""

    columns = list(pq.ParquetFile(path).schema_arrow.names)
    seen = 0
    ok = 0
    images = 0
    reasons: Counter[str] = Counter()
    sample_text = ""
    for _index, row in iter_parquet_rows_from(path, 0, batch_size=rows):
        result = parse_row(row, source_id="probe")
        seen += 1
        if result.accepted and result.sample is not None:
            ok += 1
            images = max(images, len(result.sample.image_bytes_list))
            sample_text = sample_text or _preview(result.sample)
        else:
            reasons[result.reason or "unknown"] += 1
        if seen >= rows:
            break

    if seen == 0:
        return Probe("empty", source=str(path), columns=columns)
    verdict = "ok" if ok else reasons.most_common(1)[0][0]
    if verdict not in VERDICTS:
        verdict = "missing_text"
    return Probe(
        verdict=verdict,
        source=str(path),
        columns=columns,
        rows_ok=ok,
        rows_seen=seen,
        images=images,
        sample_text=sample_text,
    )


def probe_zip(path: Path, rows: int = 5) -> Probe:
    """Probe the first parquet member of a zip, or explain what is in there instead."""

    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        parquets = sorted(name for name in members if name.lower().endswith(".parquet"))
        if not parquets:
            return Probe("unreadable", detail=f"{path.name} 里没有 parquet，{_extensions(members)}")
        with tempfile.TemporaryDirectory() as workdir:
            extracted = Path(workdir) / "member.parquet"
            with archive.open(parquets[0]) as source, extracted.open("wb") as target:
                target.write(source.read())
            probe = probe_parquet(extracted, rows)
    probe.source = f"{path.name}!{parquets[0]}"
    return probe


def probe_dataset(directory: Path, rows: int = 5) -> Probe:
    """Probe a collection directory, preferring bare parquet over zipped parquet."""

    parquet = next(iter(sorted(directory.rglob("*.parquet"))), None)
    if parquet is not None:
        probe = probe_parquet(parquet, rows)
        probe.source = str(parquet.relative_to(directory))
        return probe

    zips = sorted(directory.rglob("*.zip"))
    if not zips:
        listing = [item for item in directory.rglob("*") if item.is_file()]
        return Probe("unreadable", detail=f"没有 parquet 也没有 zip，{_extensions(str(i) for i in listing)}")
    # Only the first zip is opened: a collection mixes formats far less often
    # than it holds hundreds of shards, and opening every one is slow.
    return probe_zip(zips[0], rows)


def _preview(sample, width: int = 60) -> str:
    parts = []
    for turn in sample.turns[:2]:
        text = " ".join(turn.text.split())
        if len(text) > width:
            text = text[:width] + "…"
        parts.append(f"{turn.role}「{text}」")
    return " → ".join(parts)


def _extensions(names) -> str:
    kinds: Counter[str] = Counter()
    for name in names:
        kinds[Path(name).suffix.lower() or "(no ext)"] += 1
    if not kinds:
        return "目录是空的"
    return "里面是 " + ", ".join(f"{ext}×{count}" for ext, count in kinds.most_common(4))


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
