from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import extract_parquets_from_zip
from .config_loader import DatasetRequest, ZipTaskConfig
from .dataset_registry import RegisteredDataset, load_dataset_registry, resolve_dataset_selection
from .image_store import ImageStore, _safe_path_part
from .json_io import (
    append_jsonl,
    iter_json_records,
    jsonl_to_json_array,
    load_completed_ids,
    prune_failed,
    truncate_file,
)
from .parquet_reader import parquet_num_rows, stream_parquet_rows
from .sample_parser import parse_row
from .translation_job import TranslationTask
from .translator import build_sharegpt_record


def should_translate_to_chinese(sample_id: str, chinese_ratio: float, seed: int) -> bool:
    if chinese_ratio <= 0:
        return False
    if chinese_ratio >= 1:
        return True
    digest = hashlib.sha256(f"{seed}:{sample_id}".encode("utf-8")).hexdigest()
    value = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
    return value < chinese_ratio


@dataclass(frozen=True)
class ParsedZipRow:
    dataset: RegisteredDataset
    request: DatasetRequest
    parquet_name: str
    row_index: int
    sample_id: str
    sample: Any
    image_paths: list[str]
    stats: dict[str, int]
    progress: Any


def run_export_zips(config: ZipTaskConfig, progress_factory: Any | None = None) -> dict[str, Any]:
    image_store, datasets, completed_ids = _prepare_zip_run(config, truncate_failed=False)
    totals: dict[str, Any] = {"processed": 0, "written": 0, "rejected": 0, "skipped": 0, "datasets": {}}

    with tempfile.TemporaryDirectory(prefix="finevision-to-sharegpt-export-") as tmp:
        for item in _iter_dataset_rows(
            config=config,
            datasets=datasets,
            image_store=image_store,
            completed_ids=completed_ids,
            totals=totals,
            progress_factory=progress_factory,
            tmp_root=Path(tmp),
            description_prefix="export",
            stats_factory=lambda: {"processed": 0, "written": 0, "rejected": 0, "skipped": 0},
            limit_reached=lambda stats, limit: stats["written"] >= limit,
        ):
            record = build_sharegpt_record(item.sample, item.image_paths)
            _write_record(config, item.dataset.name, record)
            completed_ids.add(item.sample_id)
            totals["written"] += 1
            item.stats["written"] += 1
            _update_zip_progress(item.progress, item.stats)

    _finalize_zip_run(config, datasets, totals, prune_failures=False)
    return totals


def run_translate_zips(
    config: ZipTaskConfig,
    backend_pool: Any,
    handler: Any,
    progress_factory: Any | None = None,
) -> dict[str, Any]:
    image_store, datasets, completed_ids = _prepare_zip_run(config, truncate_failed=True)
    raw_done = _prepare_raw_outputs(config, datasets) if config.emit_raw else {}
    totals: dict[str, Any] = {
        "processed": 0,
        "written": 0,
        "english": 0,
        "chinese": 0,
        "rejected": 0,
        "failed": 0,
        "skipped": 0,
        "datasets": {},
    }

    def chinese_tasks() -> Any:
        with tempfile.TemporaryDirectory(prefix="finevision-to-sharegpt-translate-") as tmp:
            for item in _iter_dataset_rows(
                config=config,
                datasets=datasets,
                image_store=image_store,
                completed_ids=completed_ids,
                totals=totals,
                progress_factory=progress_factory,
                tmp_root=Path(tmp),
                description_prefix="translate",
                stats_factory=lambda: {
                    "processed": 0,
                    "written": 0,
                    "english": 0,
                    "chinese": 0,
                    "rejected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                limit_reached=lambda stats, limit: stats["written"] + stats["chinese"] >= limit,
            ):
                if config.emit_raw and item.sample_id not in raw_done[item.dataset.name]:
                    append_jsonl(
                        _raw_output_path(config, item.dataset.name),
                        build_sharegpt_record(item.sample, item.image_paths),
                    )
                    raw_done[item.dataset.name].add(item.sample_id)
                ratio = item.request.chinese_ratio if item.request.chinese_ratio is not None else config.chinese_ratio
                if should_translate_to_chinese(item.sample_id, ratio, config.seed):
                    totals["chinese"] += 1
                    item.stats["chinese"] += 1
                    _update_zip_progress(item.progress, item.stats)
                    yield TranslationTask(
                        id=item.sample_id,
                        sample=item.sample,
                        image_paths=item.image_paths,
                        metadata={
                            "dataset": item.dataset.name,
                            "source": str(item.dataset.zip_path),
                            "parquet": item.parquet_name,
                        },
                    )
                else:
                    record = build_sharegpt_record(item.sample, item.image_paths)
                    _write_record(config, item.dataset.name, record)
                    completed_ids.add(item.sample_id)
                    totals["written"] += 1
                    totals["english"] += 1
                    item.stats["written"] += 1
                    item.stats["english"] += 1
                    _update_zip_progress(item.progress, item.stats)

    for result in backend_pool.map_unordered(chinese_tasks(), handler):
        task = result.item
        dataset_name = (task.metadata or {}).get("dataset")
        dataset_stats = totals["datasets"].get(dataset_name) if dataset_name else None
        if not result.ok:
            if config.failed_path is not None:
                append_jsonl(
                    config.failed_path,
                    {"id": task.id, "error": result.error, "backend": result.backend_name, **(task.metadata or {})},
                )
            totals["failed"] += 1
            if dataset_stats is not None:
                dataset_stats["failed"] += 1
            continue
        _write_record(config, dataset_name, result.value)
        totals["written"] += 1
        if dataset_stats is not None:
            dataset_stats["written"] += 1

    _finalize_zip_run(config, datasets, totals, prune_failures=True)
    return totals


def _prepare_zip_run(
    config: ZipTaskConfig,
    truncate_failed: bool,
) -> tuple[ImageStore, list[tuple[RegisteredDataset, DatasetRequest]], set[str]]:
    image_store = _image_store_from_images_root(config.images_root)
    registry = load_dataset_registry(config.dataset_registry)
    datasets = _selected_datasets(registry, config.datasets)
    if not config.resume:
        truncate_file(config.output_jsonl)
        for dataset, _request in datasets:
            ds_jsonl, _ds_json = _dataset_output_paths(config, dataset.name)
            truncate_file(ds_jsonl)
        if truncate_failed and config.failed_path is not None:
            truncate_file(config.failed_path)
        if config.rejected_path is not None:
            truncate_file(config.rejected_path)
    elif config.output_jsonl.exists():
        _backfill_dataset_jsonls(config, datasets)
    completed_ids = load_completed_ids([config.output_jsonl]) if config.resume else set()
    return image_store, datasets, completed_ids


def _iter_dataset_rows(
    config: ZipTaskConfig,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
    image_store: ImageStore,
    completed_ids: set[str],
    totals: dict[str, Any],
    progress_factory: Any | None,
    tmp_root: Path,
    description_prefix: str,
    stats_factory: Any,
    limit_reached: Any,
) -> Any:
    for dataset, request in datasets:
        dataset_stats = stats_factory()
        limit = request.limit if request.limit is not None else config.limit_per_dataset
        for parquet in extract_parquets_from_zip(dataset.zip_path, tmp_root):
            progress = _progress_rows(
                stream_parquet_rows(parquet.path),
                progress_factory,
                parquet.path,
                f"{description_prefix} {dataset.name}/{parquet.name}",
            )
            for row_index, row in enumerate(progress):
                if limit is not None and limit_reached(dataset_stats, limit):
                    break
                sample_id = f"{dataset.zip_path.stem}:{parquet.name}:{row_index}"
                if sample_id in completed_ids:
                    totals["skipped"] += 1
                    dataset_stats["skipped"] += 1
                    _update_zip_progress(progress, dataset_stats)
                    continue
                totals["processed"] += 1
                dataset_stats["processed"] += 1
                parsed = parse_row(row, source_id=sample_id)
                if not parsed.accepted:
                    _write_reject(config.rejected_path, dataset, parquet.name, row_index, parsed)
                    totals["rejected"] += 1
                    dataset_stats["rejected"] += 1
                    _update_zip_progress(progress, dataset_stats)
                    continue
                image_paths = [
                    image_store.save(image_bytes, dataset_name=dataset.name)
                    for image_bytes in parsed.sample.image_bytes_list
                ]
                yield ParsedZipRow(
                    dataset=dataset,
                    request=request,
                    parquet_name=parquet.name,
                    row_index=row_index,
                    sample_id=sample_id,
                    sample=parsed.sample,
                    image_paths=image_paths,
                    stats=dataset_stats,
                    progress=progress,
                )
            if limit is not None and limit_reached(dataset_stats, limit):
                break
        totals["datasets"][dataset.name] = dataset_stats


def _finalize_zip_run(
    config: ZipTaskConfig,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
    totals: dict[str, Any],
    prune_failures: bool,
) -> None:
    jsonl_to_json_array(config.output_jsonl, config.output_json)
    for dataset, _request in datasets:
        ds_jsonl, ds_json = _dataset_output_paths(config, dataset.name)
        if ds_jsonl.exists():
            jsonl_to_json_array(ds_jsonl, ds_json)
    if prune_failures and config.failed_path is not None:
        prune_failed(config.failed_path, load_completed_ids([config.output_jsonl]))
    if config.report_path is not None:
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        config.report_path.write_text(json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _selected_datasets(
    registry: Any,
    requests: list[DatasetRequest],
) -> list[tuple[RegisteredDataset, DatasetRequest]]:
    if len(requests) == 1 and requests[0].name == "*":
        selected = resolve_dataset_selection(registry, ["*"])
        request_by_name = {item.name: DatasetRequest(name=item.name) for item in selected}
    else:
        selected = resolve_dataset_selection(registry, [request.name for request in requests])
        request_by_name = {request.name: request for request in requests}
    return [(dataset, request_by_name.get(dataset.name, DatasetRequest(name=dataset.name))) for dataset in selected]


def _image_store_from_images_root(images_root: Path) -> ImageStore:
    return ImageStore(output_root=images_root.parent, images_dir=images_root.name)


def _dataset_output_paths(config: ZipTaskConfig, dataset_name: str) -> tuple[Path, Path]:
    """Per-dataset jsonl/json paths: ``<out>/<dataset>/<name>`` next to the combined files."""
    safe = _safe_path_part(dataset_name)
    ds_jsonl = config.output_jsonl.parent / safe / config.output_jsonl.name
    ds_json = config.output_json.parent / safe / config.output_json.name
    return ds_jsonl, ds_json


def _raw_output_path(config: ZipTaskConfig, dataset_name: str) -> Path:
    safe = _safe_path_part(dataset_name)
    return config.output_jsonl.parent / safe / "raw.jsonl"


def _prepare_raw_outputs(
    config: ZipTaskConfig,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
) -> dict[str, set[str]]:
    raw_done: dict[str, set[str]] = {}
    for dataset, _request in datasets:
        path = _raw_output_path(config, dataset.name)
        if config.resume:
            raw_done[dataset.name] = load_completed_ids([path])
        else:
            truncate_file(path)
            raw_done[dataset.name] = set()
    return raw_done


def _write_record(config: ZipTaskConfig, dataset_name: str | None, record: dict[str, Any]) -> None:
    """Write a record to the combined jsonl and, when known, its per-dataset jsonl."""
    append_jsonl(config.output_jsonl, record)
    if dataset_name:
        ds_jsonl, _ds_json = _dataset_output_paths(config, dataset_name)
        append_jsonl(ds_jsonl, record)


def _dataset_safe_name_of_record(record: dict[str, Any], images_dir: str) -> str | None:
    """Recover the (sanitized) dataset name from a record's ``images/<dataset>/...`` path."""
    images = record.get("images") or []
    if not images:
        return None
    parts = str(images[0]).replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == images_dir:
        return parts[1]
    return None


def _backfill_dataset_jsonls(config: ZipTaskConfig, datasets: list[tuple[RegisteredDataset, DatasetRequest]]) -> None:
    images_dir = config.images_root.name
    ds_jsonl_by_safe: dict[str, Path] = {}
    existing_by_safe: dict[str, set[str]] = {}
    for dataset, _request in datasets:
        safe = _safe_path_part(dataset.name)
        ds_jsonl, _ds_json = _dataset_output_paths(config, dataset.name)
        ds_jsonl_by_safe[safe] = ds_jsonl
        existing_by_safe[safe] = {
            str(record["id"])
            for record in iter_json_records(ds_jsonl)
            if record.get("id") is not None
        }
    for record in iter_json_records(config.output_jsonl):
        safe = _dataset_safe_name_of_record(record, images_dir)
        if safe is None or safe not in ds_jsonl_by_safe:
            continue
        record_id = record.get("id")
        if record_id is None:
            continue
        record_id = str(record_id)
        if record_id in existing_by_safe[safe]:
            continue
        append_jsonl(ds_jsonl_by_safe[safe], record)
        existing_by_safe[safe].add(record_id)


def _progress_rows(rows: Any, progress_factory: Any | None, parquet_path: Path, description: str) -> Any:
    if progress_factory is None:
        return rows
    try:
        total = parquet_num_rows(parquet_path)
    except Exception:
        total = None
    return progress_factory(rows, total=total, desc=description, unit="row")


def _update_zip_progress(progress: Any, stats: dict[str, int]) -> None:
    if hasattr(progress, "set_postfix"):
        progress.set_postfix(
            processed=stats.get("processed", 0),
            written=stats.get("written", 0),
            english=stats.get("english", 0),
            chinese=stats.get("chinese", 0),
            rejected=stats.get("rejected", 0),
            failed=stats.get("failed", 0),
            skipped=stats.get("skipped", 0),
        )


def _write_reject(
    rejected_path: Path | None,
    dataset: RegisteredDataset,
    parquet_name: str,
    row_index: int,
    parsed: Any,
) -> None:
    if rejected_path is None:
        return
    append_jsonl(
        rejected_path,
        {
            "source": str(dataset.zip_path),
            "dataset": dataset.name,
            "parquet": parquet_name,
            "row_index": row_index,
            "reason": parsed.reason,
            **parsed.metadata,
        },
    )
