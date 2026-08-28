from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config_loader import ZipTaskConfig, load_zip_task_config
from .dataset_registry import load_dataset_registry
from .db import MysqlConfig
from .json_io import append_jsonl, jsonl_to_json_array, truncate_file


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
    return {
        "written": written,
        "output_jsonl": str(output_jsonl),
        "output_json": str(output_json),
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))
