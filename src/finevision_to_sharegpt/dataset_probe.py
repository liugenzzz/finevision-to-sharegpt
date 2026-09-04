from __future__ import annotations

import os
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq

from .parquet_reader import iter_parquet_rows_from
from .sample_parser import parse_row

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .dataset_registry import RegisteredDataset

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


# Enough files to name what a directory is full of. A collection with no parquet
# can still hold millions of images, and listing all of them to print
# ".jpg×2000000" says nothing the first few hundred did not.
_CENSUS_LIMIT = 500


def _first_file(directory: Path, suffix: str) -> Path | None:
    """First file with ``suffix``, in a stable order, without walking the whole tree.

    ``sorted(rglob(...))`` materialises every match before picking one, so
    probing a multi-terabyte collection meant a full recursive stat of the tree
    just to read five rows — on a network mount that reads as a hang. Walking
    top-down in sorted order reaches a representative shard and stops there.
    """

    for current, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(suffix):
                return Path(current) / name
    return None


def probe_dataset(directory: Path, rows: int = 5) -> Probe:
    """Probe a collection directory, preferring bare parquet over zipped parquet."""

    parquet = _first_file(directory, ".parquet")
    if parquet is not None:
        probe = probe_parquet(parquet, rows)
        probe.source = str(parquet.relative_to(directory))
        return probe

    # Only the first zip is opened: a collection mixes formats far less often
    # than it holds hundreds of shards, and opening every one is slow.
    archive = _first_file(directory, ".zip")
    if archive is not None:
        return probe_zip(archive, rows)

    listing: list[str] = []
    for current, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        listing.extend(filenames)
        if len(listing) >= _CENSUS_LIMIT:
            break
    return Probe("unreadable", detail=f"没有 parquet 也没有 zip，{_extensions(listing)}")


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


def verify_datasets(
    datasets: Mapping[str, "RegisteredDataset"], rows: int = 5
) -> tuple[dict[str, "RegisteredDataset"], dict[str, Probe]]:
    """Split registered datasets into the readable ones and the rest.

    Holding a parquet file is a weak test: a text-only set passes it and then
    contributes nothing but rejected rows, which still cost a full read on every
    scan and can never be retried because ``rejected`` is terminal. Parsing a few
    real rows with the pipeline's own parser is exactly the check the ingest
    performs, so a registry built on it cannot register anything the ingest will
    throw away.
    """

    kept: dict[str, Any] = {}
    dropped: dict[str, Probe] = {}
    for name in sorted(datasets):
        dataset = datasets[name]
        try:
            probe = probe_dataset(dataset.source_path, rows)
        except Exception as exc:  # noqa: BLE001 - one bad dataset must not stop the sweep
            probe = Probe("unreadable", detail=f"打开失败: {exc}")
        if probe.verdict == "ok":
            kept[name] = dataset
        else:
            dropped[name] = probe
    return kept, dropped
