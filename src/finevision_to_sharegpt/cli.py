from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from tqdm import tqdm

from .backend_pool import TranslationBackendPool
from .concurrency import DynamicLimiter, resolve_concurrency
from .config_loader import load_backend_config, load_translate_json_config, load_zip_task_config
from .db_commands import (
    prompt_version,
    run_db_export,
    run_db_init,
    run_db_status,
    run_list_datasets,
)
from .json_io import append_jsonl, iter_json_records, merge_jsonl_files, truncate_file
from .qwen_client import QwenClient
from .translation_job import TranslationTask, record_to_source_sample, run_translation_job_with_backend_pool
from .translator import DEFAULT_SAMPLE_PROMPT, DEFAULT_UTTERANCE_PROMPT, load_prompt, translate_sample
from .validator import iter_records, validate_file
from .zip_pipeline import run_export_zips as run_export_zips_config
from .zip_pipeline import run_scan_zips as run_scan_zips_config
from .zip_pipeline import run_translate_zips as run_translate_zips_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finevision-to-sharegpt")
    subparsers = parser.add_subparsers(dest="command", required=True)

    translate_json = subparsers.add_parser("translate-json", help="translate existing English ShareGPT JSON/JSONL")
    translate_json.add_argument("--config")
    translate_json.add_argument("--input")
    translate_json.add_argument("--output")
    translate_json.add_argument("--images-root")
    translate_json.add_argument("--sample-prompt-file")
    translate_json.add_argument("--utterance-prompt-file")
    translate_json.add_argument("--model", default=os.getenv("JUDGE_MODEL", "Qwen3-VL-235B-A22B-Instruct"))
    translate_json.add_argument(
        "--api-base",
        default=os.getenv("JUDGE_API_BASE", "http://192.168.48.2:18180/v1/chat/completions"),
    )
    translate_json.add_argument("--api-key", default=os.getenv("JUDGE_API_KEY", os.getenv("OPENAI_API_KEY", "sk-local")))
    translate_json.add_argument("--timeout", type=int, default=120)
    translate_json.add_argument("--concurrency", default="auto")
    translate_json.add_argument("--min-concurrency", type=int, default=24)
    translate_json.add_argument("--max-concurrency", type=int, default=60)
    translate_json.add_argument("--resume", action="store_true")
    translate_json.add_argument("--done-path")
    translate_json.add_argument("--failed-path")
    translate_json.add_argument("--progress", dest="progress", action="store_true", default=True)
    translate_json.add_argument("--no-progress", dest="progress", action="store_false")

    translate_zips = subparsers.add_parser("translate-zips", help="translate registered zip datasets by ratio")
    translate_zips.add_argument("--config", required=True)

    export_zips = subparsers.add_parser("export-zips", help="export registered zip datasets without translation")
    export_zips.add_argument("--config", required=True)

    validate = subparsers.add_parser("validate", help="validate ShareGPT JSON/JSONL")
    validate.add_argument("--input", required=True)
    validate.add_argument("--output", required=True)
    validate.add_argument("--rejects", required=True)

    merge = subparsers.add_parser("merge", help="merge JSONL files with stable id deduplication")
    merge.add_argument("--inputs", nargs="+", required=True)
    merge.add_argument("--output", required=True)

    list_datasets = subparsers.add_parser(
        "list-datasets", help="show which datasets a registry resolves to"
    )
    list_datasets.add_argument("--config", required=True)
    list_datasets.add_argument("--limit", type=int)

    db_init = subparsers.add_parser("db-init", help="create MySQL tables and per-dataset views")
    db_init.add_argument("--config", required=True)

    db_scan = subparsers.add_parser("db-scan", help="ingest source rows into MySQL without translating")
    db_scan.add_argument("--config", required=True)

    db_export = subparsers.add_parser("db-export", help="export finished rows from MySQL as ShareGPT JSONL")
    db_export.add_argument("--config", required=True)
    db_export.add_argument("--output", required=True)
    db_export.add_argument("--dataset")
    db_export.add_argument("--batch-id")
    db_export.add_argument("--lang", choices=["zh", "en"])

    db_status = subparsers.add_parser("db-status", help="show per-dataset row counts by status")
    db_status.add_argument("--config", required=True)
    db_status.add_argument("--dataset")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        raise
    if args.command == "validate":
        total, kept, rejected = validate_file(args.input, args.output, args.rejects)
        print(f"total={total} kept={kept} rejected={rejected}")
        return 0
    if args.command == "merge":
        stats = merge_jsonl_files([Path(item) for item in args.inputs], Path(args.output))
        print(
            f"read={stats['read']} written={stats['written']} "
            f"duplicates={stats['duplicates']} output={args.output}"
        )
        return 0
    if args.command == "translate-json":
        stats = run_translate_json_config(args.config) if args.config else run_translate_json(args)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "translate-zips":
        stats = run_translate_zips_from_config(args.config)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "export-zips":
        stats = run_export_zips_config(load_zip_task_config(args.config), progress_factory=tqdm)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "list-datasets":
        print(json.dumps(run_list_datasets(args.config, args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.command == "db-init":
        print(json.dumps(run_db_init(args.config), ensure_ascii=False))
        return 0
    if args.command == "db-scan":
        stats = run_scan_zips_config(load_zip_task_config(args.config), progress_factory=tqdm)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "db-export":
        stats = run_db_export(
            args.config,
            args.output,
            dataset=args.dataset,
            batch_id=args.batch_id,
            lang=args.lang,
        )
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "db-status":
        print(json.dumps(run_db_status(args.config, args.dataset), ensure_ascii=False))
        return 0
    raise ValueError(f"unknown command: {args.command}")


def run_translate_json_config(config_path: Path | str) -> dict[str, Any]:
    config = load_translate_json_config(config_path)
    if config.backend_config is None:
        raise ValueError("translate-json config requires backend_config")
    backend_config = load_backend_config(config.backend_config)
    pool = _make_backend_pool(backend_config)
    sample_prompt = load_prompt(config.sample_prompt_file, DEFAULT_SAMPLE_PROMPT)
    utterance_prompt = load_prompt(config.utterance_prompt_file, DEFAULT_UTTERANCE_PROMPT)

    def tasks() -> Any:
        for index, record in enumerate(iter_json_records(config.input)):
            record_id = str(record.get("id", index))
            yield TranslationTask(
                id=record_id,
                sample=record_to_source_sample(record, config.images_root),
                image_paths=[str(item) for item in _as_list(record.get("images", record.get("image")))],
            )

    def handler(task: TranslationTask, client: Any, timeout: int) -> dict[str, Any]:
        result = translate_sample(
            client,
            task.sample,
            task.image_paths,
            sample_prompt=sample_prompt,
            utterance_prompt=utterance_prompt,
            timeout=timeout,
        )
        if not result.ok:
            raise RuntimeError(result.error or "translation failed")
        return result.record

    progress = tqdm(total=sum(1 for _ in iter_json_records(config.input)), desc=config.input.name, unit="record")
    try:
        return run_translation_job_with_backend_pool(
            tasks(),
            config.output_jsonl,
            config.output_json,
            config.done_path,
            config.failed_path,
            pool,
            handler,
            resume=config.resume,
            progress=progress,
        )
    finally:
        progress.close()


def run_translate_zips_from_config(config_path: Path | str) -> dict[str, Any]:
    config = load_zip_task_config(config_path)
    if config.backend_config is None:
        raise ValueError("translate-zips config requires backend_config")
    backend_config = load_backend_config(config.backend_config)
    pool = _make_backend_pool(backend_config)
    sample_prompt = load_prompt(config.sample_prompt_file, DEFAULT_SAMPLE_PROMPT)
    utterance_prompt = load_prompt(config.utterance_prompt_file, DEFAULT_UTTERANCE_PROMPT)

    def handler(task: TranslationTask, client: Any, timeout: int) -> dict[str, Any]:
        started = time.monotonic()
        result = translate_sample(
            client,
            task.sample,
            task.image_paths,
            sample_prompt=sample_prompt,
            utterance_prompt=utterance_prompt,
            timeout=timeout,
        )
        if task.metadata is not None:
            task.metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
        if not result.ok:
            raise RuntimeError(result.error or "translation failed")
        return result.record

    return run_translate_zips_config(
        config,
        pool,
        handler,
        progress_factory=tqdm,
        translation_meta={
            "models_by_backend": {backend.name: backend.model for backend in backend_config.backends},
            "prompt_version": prompt_version(sample_prompt, utterance_prompt),
        },
    )


def _make_backend_pool(backend_config: Any) -> TranslationBackendPool:
    return TranslationBackendPool(
        backend_config,
        client_factory=lambda backend: QwenClient(backend.api_base, backend.api_key, backend.model),
    )


def run_translate_json(args: argparse.Namespace) -> dict[str, Any]:
    client = QwenClient(args.api_base, args.api_key, args.model)
    sample_prompt = load_prompt(args.sample_prompt_file, DEFAULT_SAMPLE_PROMPT)
    utterance_prompt = load_prompt(args.utterance_prompt_file, DEFAULT_UTTERANCE_PROMPT)
    images_root = Path(args.images_root)
    output_path = Path(args.output)
    done_path = Path(args.done_path) if args.done_path else output_path.with_suffix(output_path.suffix + ".done.jsonl")
    failed_path = Path(args.failed_path) if args.failed_path else output_path.with_suffix(output_path.suffix + ".failed.jsonl")
    done_records = _load_done_records(done_path) if args.resume else {}
    completed_ids = set(done_records)
    if not args.resume:
        truncate_file(done_path)
        truncate_file(failed_path)
    totals = {"processed": 0, "written": 0, "failed": 0, "skipped": 0}
    limiter = _make_limiter(args)

    records = _filter_resume_records(iter_records(args.input), completed_ids, totals)
    progress = tqdm(records, desc=Path(args.input).name, unit="record") if args.progress else records
    with JsonArrayWriter(args.output) as writer:
        for record in done_records.values():
            writer.write(record)
            totals["written"] += 1
        for index, record, failed in _translate_json_unordered(
            enumerate(progress),
            client,
            sample_prompt,
            utterance_prompt,
            images_root,
            args.timeout,
            limiter,
        ):
            totals["processed"] += 1
            if record is None:
                append_jsonl(failed_path, failed)
                totals["failed"] += 1
            else:
                writer.write(record)
                append_jsonl(done_path, record)
                totals["written"] += 1
            _update_progress(progress, totals)
    return totals


class JsonArrayWriter:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.handle: Any = None
        self.first = True

    def __enter__(self) -> "JsonArrayWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        self.handle.write("[\n")
        return self

    def write(self, record: dict[str, Any]) -> None:
        if self.handle is None:
            raise RuntimeError("writer is not open")
        if not self.first:
            self.handle.write(",\n")
        text = json.dumps(record, ensure_ascii=False, indent=2)
        self.handle.write("  " + text.replace("\n", "\n  "))
        self.first = False

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        self.handle.write("\n]\n")
        self.handle.close()
        self.handle = None


def _translate_json_unordered(
    items: Any,
    client: QwenClient,
    sample_prompt: str,
    utterance_prompt: str,
    images_root: Path,
    timeout: int,
    limiter: DynamicLimiter,
) -> Any:
    iterator = iter(items)

    def worker(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
        index, record = item
        record_id = str(record.get("id", index))
        try:
            sample = record_to_source_sample(record, images_root)
            result = translate_sample(
                client,
                sample,
                _as_list(record.get("images", record.get("image"))),
                sample_prompt=sample_prompt,
                utterance_prompt=utterance_prompt,
                timeout=timeout,
            )
            if result.ok:
                return index, result.record, {}
            return index, None, {"index": index, "id": record_id, "error": result.error}
        except Exception as exc:
            return index, None, {"index": index, "id": record_id, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=limiter.maximum) as executor:
        futures = set()

        def fill() -> None:
            while len(futures) < limiter.current:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                futures.add(executor.submit(worker, item))

        fill()
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if result[1] is None:
                    limiter.record_failure()
                else:
                    limiter.record_success()
                yield result
            fill()


def _filter_resume_records(
    records: Any,
    completed_ids: set[str],
    totals: dict[str, Any],
) -> Any:
    for record in records:
        record_id = str(record.get("id", ""))
        if record_id and record_id in completed_ids:
            totals["skipped"] += 1
            continue
        yield record


def _load_done_records(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_id = record.get("id")
            if record_id is not None:
                records[str(record_id)] = record
    return records


def _make_limiter(args: argparse.Namespace) -> DynamicLimiter:
    initial = resolve_concurrency(args.concurrency, args.min_concurrency, args.max_concurrency)
    return DynamicLimiter(initial=initial, maximum=max(1, int(args.max_concurrency)))


def _update_progress(progress: Any, totals: dict[str, Any]) -> None:
    if hasattr(progress, "set_postfix"):
        progress.set_postfix(
            written=totals.get("written", 0),
            failed=totals.get("failed", 0),
            skipped=totals.get("skipped", 0),
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


if __name__ == "__main__":
    raise SystemExit(main())
