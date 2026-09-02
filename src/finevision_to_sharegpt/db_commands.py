from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config_loader import ZipTaskConfig, load_zip_task_config
from .dataset_registry import load_dataset_registry
from .db import MysqlConfig
from .db.ledger import DatasetVersion
from .image_store import _safe_path_part
from .json_io import append_jsonl, iter_json_records, jsonl_to_json_array, truncate_file
from .translator import DEFAULT_SAMPLE_PROMPT, DEFAULT_UTTERANCE_PROMPT, load_prompt


def prompt_version(*prompts: str) -> str:
    """Short digest of the prompt set, so re-translations are attributable."""

    digest = hashlib.sha256()
    for prompt in prompts:
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def _require_mysql(config: ZipTaskConfig) -> MysqlConfig:
    if config.mysql is None:
        raise ValueError("this command requires a mysql section in the task config")
    return config.mysql


def _ledger(config: ZipTaskConfig) -> Any:
    from .db.mysql_ledger import MySQLLedger

    return MySQLLedger(_require_mysql(config), batch_id=config.batch_id)


def run_db_init(config_path: Path | str) -> dict[str, Any]:
    """Create the tables and one view per registered dataset. Idempotent."""

    config = load_zip_task_config(config_path)
    ledger = _ledger(config)
    try:
        registry = load_dataset_registry(config.dataset_registry)
        for name in sorted(registry.datasets):
            ledger.ensure_view(name)
        return {"tables": 4, "views": sorted(registry.datasets)}
    finally:
        ledger.close()


def run_db_status(config_path: Path | str, dataset: str | None = None) -> dict[str, Any]:
    config = load_zip_task_config(config_path)
    ledger = _ledger(config)
    try:
        rows = ledger.status_counts(dataset)
    finally:
        ledger.close()
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["status"]] = totals.get(row["status"], 0) + row["count"]
    return {"rows": rows, "totals": totals}


def run_db_storage(config_path: Path | str) -> dict[str, Any]:
    """Report how much space the ledger occupies, for extrapolating a big load."""

    config = load_zip_task_config(config_path)
    ledger = _ledger(config)
    try:
        return ledger.storage_report()
    finally:
        ledger.close()


def run_db_export(
    config_path: Path | str,
    output: Path | str,
    dataset: str | None = None,
    batch_id: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Write done samples back out as ShareGPT JSONL plus a JSON array."""

    config = load_zip_task_config(config_path)
    output_jsonl = Path(output)
    output_json = output_jsonl.with_name(output_jsonl.stem + ".json")
    truncate_file(output_jsonl)
    ledger = _ledger(config)
    written = 0
    try:
        for record in ledger.iter_export_records(dataset=dataset, batch_id=batch_id, lang=lang):
            append_jsonl(output_jsonl, record)
            written += 1
    finally:
        ledger.close()
    jsonl_to_json_array(output_jsonl, output_json)
    result = {
        "written": written,
        "output_jsonl": str(output_jsonl),
        "output_json": str(output_json),
    }
    if config.mysql is not None and not config.mysql.store_conversations:
        # Translations live in sample_translation and are unaffected, so only
        # the untranslated English side is missing. Saying "nothing can be
        # exported" would be wrong.
        result["note"] = (
            "mysql.store_conversations is false: translated samples export normally, "
            "but samples still in English have no source text in the ledger and are "
            "skipped. Take those from the pipeline's own jsonl output."
        )
    return result


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def run_list_datasets(config_path: Path | str, limit: int | None = None) -> dict[str, Any]:
    """Show what a registry actually resolves to, before running anything.

    Auto-discovery is easy to point one level too high, which silently
    registers the parent as a single huge dataset. Seeing the resolved list
    with its shard counts makes that obvious at a glance.
    """

    config = load_zip_task_config(config_path)
    registry = load_dataset_registry(config.dataset_registry)
    rows: list[dict[str, Any]] = []
    for name in sorted(registry.datasets):
        dataset = registry.datasets[name]
        if dataset.is_directory:
            files = [item for item in dataset.source_path.rglob("*.parquet") if item.is_file()]
            size = sum(item.stat().st_size for item in files)
        else:
            files = []
            size = dataset.source_path.stat().st_size if dataset.source_path.exists() else 0
        rows.append(
            {
                "name": name,
                "kind": dataset.kind,
                "parquet_files": len(files) if dataset.is_directory else None,
                "size_gb": round(size / 1024**3, 3),
                "path": str(dataset.source_path),
            }
        )
    shown = rows if limit is None else rows[:limit]
    return {
        "total": len(rows),
        "total_size_gb": round(sum(row["size_gb"] for row in rows), 3),
        "datasets": shown,
        "truncated": len(rows) - len(shown),
    }


def _split_sample_id(sample_id: str) -> tuple[str, str, int] | None:
    """``<源标识>:<分片路径>:<行号>`` 拆成三段。

    分片路径本身可能带斜杠（`nested/part.parquet`），但两端的源标识和行号都
    不含冒号，所以从两头切最稳妥。
    """

    head, _, tail = sample_id.rpartition(":")
    if not head or not tail.isdigit():
        return None
    source_id, _, parquet_name = head.partition(":")
    if not source_id or not parquet_name:
        return None
    return source_id, parquet_name, int(tail)


def _read_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {
        str(record["id"]): record
        for record in iter_json_records(path)
        if record.get("id") is not None
    }


def run_db_restore(
    config_path: Path | str,
    output_dir: Path | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把已产出的 JSONL 灌回账本。

    账本和产出分处两个卷时，账本可能整个丢掉而 JSONL 完好。此时若带着空库
    续跑，`resume` 一条都跳不过去，已翻的会重做一遍，还会往同一个 JSONL 里
    追加重复记录。

    只补 `sample_source` 的 `done` 行是不够的：`is_consumed` 对水位线之上的
    行直接返回未消费，所以 `dataset_cursor` 必须一起重建，否则恢复等于没做。
    """

    from .db.mysql_ledger import MySQLLedger

    config = load_zip_task_config(config_path)
    _require_mysql(config)
    root = Path(output_dir) if output_dir else config.output_jsonl.parent
    registry = load_dataset_registry(config.dataset_registry)

    prompt = prompt_version(
        load_prompt(config.sample_prompt_file, DEFAULT_SAMPLE_PROMPT),
        load_prompt(config.utterance_prompt_file, DEFAULT_UTTERANCE_PROMPT),
    )
    totals: dict[str, Any] = {"datasets": {}, "source_rows": 0, "translations": 0, "skipped_ids": 0}
    ledger = None if dry_run else MySQLLedger(_require_mysql(config), batch_id=config.batch_id)
    try:
        for name in sorted(registry.datasets):
            dataset = registry.datasets[name]
            folder = root / _safe_path_part(name)
            chinese = _read_records(folder / config.output_jsonl.name)
            english = _read_records(folder / "raw.jsonl")
            if not chinese and not english:
                continue

            version = (
                DatasetVersion(dataset=name)
                if ledger is None
                else ledger.open_dataset(name, dataset.source_path, config.images_root)
            )
            marks: dict[str, int] = {}
            restored = translated = 0
            for sample_id in sorted(set(chinese) | set(english)):
                parts = _split_sample_id(sample_id)
                if parts is None:
                    totals["skipped_ids"] += 1
                    continue
                _source_id, parquet_name, row_index = parts
                # 英文优先做源文本；只有中文时退而用中文，至少保住行的存在。
                source = english.get(sample_id) or chinese[sample_id]
                lang = "zh" if sample_id in chinese else "en"
                if ledger is not None:
                    ledger.claim(
                        version,
                        parquet_name,
                        row_index,
                        sample_id,
                        source.get("conversations") or [],
                        [str(item) for item in source.get("images") or []],
                    )
                    ledger.mark_done(version, sample_id, lang)
                restored += 1
                if lang == "zh":
                    if ledger is not None:
                        ledger.record_translation(
                            version,
                            sample_id,
                            chinese[sample_id].get("conversations") or [],
                            None,
                            "",
                            prompt,
                            None,
                        )
                    translated += 1
                if row_index > marks.get(parquet_name, -1):
                    marks[parquet_name] = row_index

            if ledger is not None:
                # 水位线必须跟着重建，否则 is_consumed 认为这些行从未被扫过。
                ledger.flush()
                for parquet_name, row_index in marks.items():
                    ledger.note_scanned(version, parquet_name, row_index)
                ledger.flush()
            totals["datasets"][name] = {"source_rows": restored, "translations": translated}
            totals["source_rows"] += restored
            totals["translations"] += translated
    finally:
        if ledger is not None:
            ledger.flush()
            ledger.close()
    totals["dry_run"] = dry_run
    return totals


def run_db_retry_rejected(
    config_path: Path | str,
    datasets: list[str] | None = None,
    reasons: list[str] | None = None,
    apply_all: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """把被拒的行放回 pending，让改好的解析器重新看一遍。

    `rejected` 是终态：解析器改好之后，已经被拒的行不会自动重来。而拒绝分两种，
    混在一起处理会出事——解析器缺陷造成的该重试，纯文本数据集没有图片字节造成的
    不该重试，放回去只会让此后每一轮都白读它们一遍。

    所以写入时要求给出数据集或原因来圈定范围；真要全量重置得显式 `apply_all`。
    不圈范围的预览是放行的——那正是拿来看「有哪些原因、各多少行」的入口。
    """

    from .db.mysql_ledger import MySQLLedger

    config = load_zip_task_config(config_path)
    if not dry_run and not datasets and not reasons and not apply_all:
        raise ValueError(
            "要重试哪些行必须说清楚：给 --dataset 或 --reason 圈定范围。\n"
            "  想看有哪些可选，去掉 --apply 跑一次，会按原因列出各有多少行。\n"
            "  确实要重置全部被拒的行，加 --all——但先确认里面没有纯文本数据集，\n"
            "  它们的拒绝是对的，放回去每一轮都会白读一遍。"
        )

    ledger = MySQLLedger(_require_mysql(config), batch_id=config.batch_id)
    try:
        before = ledger.rejected_breakdown(datasets, reasons)
        affected = sum(row["count"] for row in before)
        result: dict[str, Any] = {
            "matched": affected,
            "groups": before,
            "dry_run": dry_run,
        }
        if dry_run:
            result["note"] = "只是预览，没有改动任何行。确认无误后加 --apply。"
            return result
        result["reset"] = ledger.retry_rejected(datasets, reasons)
    finally:
        ledger.close()
    return result
