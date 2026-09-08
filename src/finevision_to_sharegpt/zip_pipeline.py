from __future__ import annotations

import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import iter_dataset_parquets
from .config_loader import DatasetRequest, ZipTaskConfig
from .dataset_registry import RegisteredDataset, load_dataset_registry, resolve_dataset_selection
from .db import ConsumptionLedger, DatasetVersion, open_ledger
from .db.ledger import JsonlLedger
from .image_store import ImageStore, _safe_path_part
from .json_io import (
    append_jsonl,
    iter_json_records,
    jsonl_to_json_array,
    load_completed_ids,
    prune_failed,
    truncate_file,
)
from .parquet_reader import iter_parquet_rows_from, parquet_num_rows
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
    version: DatasetVersion
    request: DatasetRequest
    parquet_name: str
    row_index: int
    sample_id: str
    sample: Any
    image_paths: list[str]
    stats: dict[str, int]
    progress: Any


def run_export_zips(config: ZipTaskConfig, progress_factory: Any | None = None) -> dict[str, Any]:
    image_store, datasets, ledger = _prepare_zip_run(config, truncate_failed=False)
    totals: dict[str, Any] = {"processed": 0, "written": 0, "rejected": 0, "skipped": 0, "datasets": {}}
    versions: dict[str, DatasetVersion] = {}

    with ledger, tempfile.TemporaryDirectory(prefix="finevision-to-sharegpt-export-") as tmp:
        for item in _iter_dataset_rows(
            config=config,
            datasets=datasets,
            image_store=image_store,
            ledger=ledger,
            versions=versions,
            totals=totals,
            progress_factory=progress_factory,
            tmp_root=Path(tmp),
            description_prefix="export",
            stats_factory=lambda: {"processed": 0, "written": 0, "rejected": 0, "skipped": 0},
            limit_reached=lambda stats, limit: stats["written"] + stats["skipped"] >= limit,
        ):
            record = build_sharegpt_record(item.sample, item.image_paths)
            _write_record(config, item.dataset.name, record)
            ledger.mark_done(item.version, item.sample_id, "en")
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
    translation_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image_store, datasets, ledger = _prepare_zip_run(config, truncate_failed=True)
    # The ledger already holds a MySQL connection, so nothing may fail between
    # here and the block that closes it: every leaked connection lives on for
    # wait_timeout (eight hours by default) and a handful of aborted attempts
    # is enough to exhaust max_connections.
    with ledger:
        return _run_translate_zips(
            config, backend_pool, handler, progress_factory, translation_meta,
            image_store, datasets, ledger,
        )


def _run_translate_zips(
    config: ZipTaskConfig,
    backend_pool: Any,
    handler: Any,
    progress_factory: Any | None,
    translation_meta: dict[str, Any] | None,
    image_store: ImageStore,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
    ledger: ConsumptionLedger,
) -> dict[str, Any]:
    meta = translation_meta or {}
    models_by_backend: dict[str, str] = meta.get("models_by_backend") or {}
    prompt_version: str = meta.get("prompt_version") or ""
    versions: dict[str, DatasetVersion] = {}
    progress_state: dict[str, Any] = {}
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
                ledger=ledger,
                versions=versions,
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
                limit_reached=lambda stats, limit: stats["written"] + stats["chinese"] + stats["skipped"] >= limit,
                progress_state=progress_state,
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
                            "source": str(item.dataset.source_path),
                            "parquet": item.parquet_name,
                        },
                    )
                else:
                    record = build_sharegpt_record(item.sample, item.image_paths)
                    _write_record(config, item.dataset.name, record)
                    ledger.mark_done(item.version, item.sample_id, "en")
                    totals["written"] += 1
                    totals["english"] += 1
                    item.stats["written"] += 1
                    item.stats["english"] += 1
                    _update_zip_progress(item.progress, item.stats)

    for result in backend_pool.map_unordered(chinese_tasks(), handler):
        task = result.item
        metadata = task.metadata or {}
        dataset_name = metadata.get("dataset")
        dataset_stats = totals["datasets"].get(dataset_name) if dataset_name else None
        version = versions.get(dataset_name) if dataset_name else None
        if not result.ok:
            if config.failed_path is not None:
                append_jsonl(
                    config.failed_path,
                    {
                        "id": task.id,
                        "error": result.error,
                        "backend": result.backend_name,
                        **metadata,
                    },
                )
            if version is not None:
                ledger.mark_failed(version, task.id, result.error)
            totals["failed"] += 1
            if dataset_stats is not None:
                dataset_stats["failed"] += 1
            continue
        _write_record(config, dataset_name, result.value)
        if version is not None:
            ledger.record_translation(
                version,
                task.id,
                result.value.get("conversations") or [],
                result.backend_name,
                models_by_backend.get(result.backend_name or "", ""),
                prompt_version,
                metadata.get("latency_ms"),
            )
            ledger.mark_done(version, task.id, "zh")
        totals["written"] += 1
        if dataset_stats is not None:
            dataset_stats["written"] += 1
        _update_overall_progress(progress_state.get("overall"), totals)

    _finalize_zip_run(config, datasets, totals, prune_failures=True)
    return totals


def _prepare_zip_run(
    config: ZipTaskConfig,
    truncate_failed: bool,
    prepare_outputs: bool = True,
) -> tuple[ImageStore, list[tuple[RegisteredDataset, DatasetRequest]], ConsumptionLedger]:
    """Resolve datasets and the ledger, and reset output files when not resuming.

    ``prepare_outputs=False`` leaves every output file alone; ``db-scan`` only
    fills the database and must not create or truncate a ShareGPT output.
    """

    image_store = _image_store_from_images_root(config.images_root)
    registry = load_dataset_registry(config.dataset_registry)
    datasets = _selected_datasets(registry, config.datasets)
    if not prepare_outputs:
        ledger = open_ledger(config.mysql, set(), batch_id=_batch_id(config))
        return image_store, datasets, ledger
    if not config.resume:
        truncate_file(config.output_jsonl)
        for dataset, _request in datasets:
            ds_jsonl, _ds_json = _dataset_output_paths(config, dataset.name)
            truncate_file(ds_jsonl)
        if truncate_failed and config.failed_path is not None:
            truncate_file(config.failed_path)
        if config.rejected_path is not None:
            truncate_file(config.rejected_path)
    elif config.output_jsonl.exists() and _backfill_is_needed(config, datasets):
        _backfill_dataset_jsonls(config, datasets)
    # 先开账本，看清楚拿到的是哪一种，再决定要不要读那一大坨 id。
    #
    # 只有文件账本会去查这个集合；MySQL 账本的 is_consumed 走的是 scan_plan
    # 现查回来的 consumed_ids，收下这个集合之后一次都不读（mysql_ledger.py 的
    # is_consumed）。500 万条时这一遍是 7 GB 的磁盘和 1.1 GB 的内存，全花在一个
    # 没人查的 set 上。
    #
    # 不能按「配了 MySQL」来判断：MySQL 连不上而 fail_fast 是关的时候，
    # open_ledger 会退回文件账本——那时候这个集合是续跑唯一的依据，不读就会
    # 把已经翻完的整轮重翻一遍。所以判据必须是拿到手的账本类型。
    ledger = open_ledger(config.mysql, set(), batch_id=_batch_id(config))
    if config.resume and isinstance(ledger, JsonlLedger):
        ledger.completed_ids = load_completed_ids([config.output_jsonl])
    return image_store, datasets, ledger


def _batch_id(config: ZipTaskConfig) -> str:
    return config.batch_id or time.strftime("%Y%m%d-%H%M%S")


# ``limit`` counts what a dataset contributes in total, not what one run adds.
# Rows an earlier run already finished come back as ``skipped``, so they have to
# count too: without that, every restart translates a fresh ``limit`` on top of
# what is already in the output, and a long run that gets interrupted a few
# times quietly ends up with several times the planned quota.


def _iter_dataset_rows(
    config: ZipTaskConfig,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
    image_store: ImageStore,
    ledger: ConsumptionLedger,
    versions: dict[str, DatasetVersion],
    totals: dict[str, Any],
    progress_factory: Any | None,
    tmp_root: Path,
    description_prefix: str,
    stats_factory: Any,
    limit_reached: Any,
    claim_status: str = "claimed",
    progress_state: dict[str, Any] | None = None,
    write_images: bool = True,
    for_ingest: bool = False,
) -> Any:
    overall = _overall_progress(datasets, progress_factory, description_prefix)
    if progress_state is not None:
        progress_state["overall"] = overall
    for dataset, request in datasets:
        _describe_overall(overall, dataset.name, len(datasets))
        dataset_stats = stats_factory()
        limit = request.limit if request.limit is not None else config.limit_per_dataset
        version = ledger.open_dataset(
            dataset.name, dataset.source_path, config.images_root, dataset.source_lang
        )
        versions[dataset.name] = version
        for parquet in iter_dataset_parquets(dataset, tmp_root):
            plan = ledger.scan_plan(version, parquet.name, for_ingest=for_ingest)
            # 水位线之下的行不会被读到，配额得由账本补报，否则续跑会超额。
            if plan.skipped_before:
                totals["skipped"] += plan.skipped_before
                dataset_stats["skipped"] += plan.skipped_before
            # 跳过与否只看行号——sample_id 是 <数据集>:<分片>:<行号>，跟行内容
            # 无关。交给读取器，它就能在解码前判断，整组整批地跳过去；不然
            # 每一行都要连图片字节一起 materialize 出来再丢掉，7000 行/秒。
            def already_done(row_index: int, _plan=plan, _version=version, _parquet=parquet) -> bool:
                sample_id = f"{dataset.source_id}:{_parquet.name}:{row_index}"
                return ledger.is_consumed(_version, sample_id, row_index, _plan)

            progress = _progress_rows(
                iter_parquet_rows_from(
                    parquet.path, start_row=plan.start_row, skip=already_done
                ),
                progress_factory,
                parquet.path,
                f"{description_prefix} {dataset.name}/{parquet.name}",
                start_row=plan.start_row,
            )
            exhausted = True
            for row_index, row in progress:
                if limit is not None and limit_reached(dataset_stats, limit):
                    exhausted = False
                    break
                sample_id = f"{dataset.source_id}:{parquet.name}:{row_index}"
                # row 是 None 就是读取器替我们跳掉的那些，账照记。
                if row is None:
                    totals["skipped"] += 1
                    dataset_stats["skipped"] += 1
                    ledger.note_scanned(version, parquet.name, row_index)
                    _update_zip_progress(progress, dataset_stats)
                    continue
                totals["processed"] += 1
                dataset_stats["processed"] += 1
                parsed = parse_row(row, source_id=sample_id)
                if not parsed.accepted:
                    _write_reject(config.rejected_path, dataset, parquet.name, row_index, parsed)
                    ledger.mark_rejected(version, parquet.name, row_index, sample_id, parsed.reason)
                    ledger.note_scanned(version, parquet.name, row_index)
                    totals["rejected"] += 1
                    dataset_stats["rejected"] += 1
                    _update_zip_progress(progress, dataset_stats)
                    continue
                image_paths = [
                    image_store.save(image_bytes, dataset_name=dataset.name, write=write_images)
                    for image_bytes in parsed.sample.image_bytes_list
                ]
                ledger.claim(
                    version,
                    parquet.name,
                    row_index,
                    sample_id,
                    [{"from": turn.role, "value": turn.text} for turn in parsed.sample.turns],
                    image_paths,
                    status=claim_status,
                )
                ledger.note_scanned(version, parquet.name, row_index)
                yield ParsedZipRow(
                    dataset=dataset,
                    version=version,
                    request=request,
                    parquet_name=parquet.name,
                    row_index=row_index,
                    sample_id=sample_id,
                    sample=parsed.sample,
                    image_paths=image_paths,
                    stats=dataset_stats,
                    progress=progress,
                )
            if exhausted:
                ledger.finish_parquet(version, parquet.name)
            if limit is not None and limit_reached(dataset_stats, limit):
                break
        totals["datasets"][dataset.name] = dataset_stats
        _advance_overall(overall, totals)
    _close_overall(overall)


def _overall_progress(
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
    progress_factory: Any | None,
    description_prefix: str,
) -> Any:
    """Outer bar over datasets, advanced by hand rather than by iteration.

    A FineVision tree is hundreds of datasets of dozens of shards each; a bar
    per shard alone scrolls for thousands of lines and never shows how far
    along the whole run is.

    Driving it manually matters: tqdm's own iterator defers ``self.n`` behind
    ``miniters``, so a redraw triggered by ``set_postfix`` in between renders
    a stale count. Passing no iterable and calling ``update`` per finished
    dataset keeps the number honest at every redraw.
    """

    if progress_factory is None:
        return None
    return progress_factory(
        None,
        total=len(datasets),
        desc=f"{description_prefix} datasets",
        unit="dataset",
        leave=True,
        position=0,
    )


def _describe_overall(progress: Any, dataset_name: str, count: int) -> None:
    if hasattr(progress, "set_description"):
        progress.set_description(f"[{dataset_name}] of {count} datasets")


def _update_overall_progress(progress: Any, totals: dict[str, Any]) -> None:
    if hasattr(progress, "set_postfix"):
        progress.set_postfix(
            written=totals.get("written", 0),
            failed=totals.get("failed", 0),
            rejected=totals.get("rejected", 0),
            skipped=totals.get("skipped", 0),
        )


def _advance_overall(progress: Any, totals: dict[str, Any]) -> None:
    _update_overall_progress(progress, totals)
    if hasattr(progress, "update"):
        progress.update(1)


def _close_overall(progress: Any) -> None:
    if hasattr(progress, "close"):
        progress.close()


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


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _backfill_is_needed(
    config: ZipTaskConfig, datasets: list[tuple[RegisteredDataset, DatasetRequest]]
) -> bool:
    """分数据集的 jsonl 是不是真的落后于合并的那份。

    backfill 是一次性迁移：`_write_record` 一直是同时往两边写的，所以稳态下
    它读完几百 MB 之后追加 0 条。以前每次重启都白跑一遍，500 万条时光这一步
    就是十几 GB 的读。

    判据用**字节数**而不是行数，因为字节数不用读文件——166 个数据集加起来
    一次 stat 就够，而读一遍是十几 GB。这是准确的：`_write_record` 往两边写的
    是同一行字节，backfill 也只从 combined 逐行复制过去，所以分文件的内容永远
    是 combined 的子集；子集的字节数等于全集，就说明一条都不缺。

    只选了部分数据集跑的时候两者本来就不等，那就照旧跑 backfill——不会更慢，
    只是没有这个加速。
    """

    combined = _file_size(config.output_jsonl)
    if combined == 0:
        return False
    per_dataset = sum(
        _file_size(_dataset_output_paths(config, dataset.name)[0])
        for dataset, _request in datasets
    )
    return per_dataset < combined


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


def _progress_rows(
    rows: Any,
    progress_factory: Any | None,
    parquet_path: Path,
    description: str,
    start_row: int = 0,
) -> Any:
    if progress_factory is None:
        return rows
    try:
        total = max(0, parquet_num_rows(parquet_path) - start_row)
    except Exception:
        total = None
    # leave=False so hundreds of shard bars collapse instead of scrolling.
    return progress_factory(rows, total=total, desc=description, unit="row", leave=False, position=1)


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
            "source": str(dataset.source_path),
            "dataset": dataset.name,
            "parquet": parquet_name,
            "row_index": row_index,
            "reason": parsed.reason,
            **parsed.metadata,
        },
    )


def run_scan_zips(
    config: ZipTaskConfig,
    progress_factory: Any | None = None,
    write_images: bool = True,
) -> dict[str, Any]:
    """Ingest source samples into MySQL without producing ShareGPT output.

    Rows land as ``pending`` so a later ``translate-zips`` or ``export-zips``
    run claims them normally.

    ``write_images=False`` records the image paths but leaves the files
    unwritten. Paths are content hashes derived from bytes already in memory,
    so they stay correct, and whichever run later consumes a sample writes
    its images then. On a tree where only a fraction of the samples will ever
    be used, that avoids materialising terabytes nobody reads.
    """

    if config.mysql is None:
        raise ValueError("db-scan requires a mysql section in the task config")
    image_store, datasets, ledger = _prepare_zip_run(
        config, truncate_failed=False, prepare_outputs=False
    )
    totals: dict[str, Any] = {"processed": 0, "written": 0, "rejected": 0, "skipped": 0, "datasets": {}}
    versions: dict[str, DatasetVersion] = {}

    with ledger, tempfile.TemporaryDirectory(prefix="finevision-to-sharegpt-scan-") as tmp:
        for item in _iter_dataset_rows(
            config=config,
            datasets=datasets,
            image_store=image_store,
            ledger=ledger,
            versions=versions,
            totals=totals,
            progress_factory=progress_factory,
            tmp_root=Path(tmp),
            description_prefix="scan",
            stats_factory=lambda: {"processed": 0, "written": 0, "rejected": 0, "skipped": 0},
            limit_reached=lambda stats, limit: stats["written"] + stats["skipped"] >= limit,
            claim_status="pending",
            write_images=write_images,
            for_ingest=True,
        ):
            totals["written"] += 1
            item.stats["written"] += 1
            _update_zip_progress(item.progress, item.stats)

    if config.report_path is not None:
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        config.report_path.write_text(json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return totals
