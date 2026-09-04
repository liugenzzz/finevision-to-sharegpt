from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db.config import MysqlConfig, load_mysql_config


@dataclass(frozen=True)
class BackendSpec:
    name: str
    api_base: str
    model: str
    api_key: str
    concurrency: int
    weight: float = 1.0
    extra_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackendPoolConfig:
    """How long the pool is willing to wait, and on what.

    ``request_timeout`` bounds one call. The two fallback limits bound the whole
    sample: without them the per-utterance retry path is one full-timeout call
    per turn with no cap on turns, so a single long conversation can hold a
    worker for hours.
    """

    backends: list[BackendSpec]
    # ``request_timeout`` 和 ``fallback_budget_seconds`` 被 Claude 1 的后端吞吐
    # 诊断脚本按名字读（有 getattr 兜底，改名不会崩，但上限会被算少而无人察觉）。
    # 要改名先说一声。
    request_timeout: int = 120
    max_retries: int = 2
    disable_backend_after_failures: int = 20
    fallback_budget_seconds: int = 300
    fallback_max_turns: int = 12


@dataclass(frozen=True)
class TranslateJsonConfig:
    input: Path
    output_jsonl: Path
    output_json: Path
    images_root: Path
    backend_config: Path | None = None
    done_path: Path | None = None
    failed_path: Path | None = None
    sample_prompt_file: Path | None = None
    utterance_prompt_file: Path | None = None
    resume: bool = True


@dataclass(frozen=True)
class DatasetRequest:
    name: str
    chinese_ratio: float | None = None
    limit: int | None = None


@dataclass(frozen=True)
class ZipTaskConfig:
    dataset_registry: Path
    datasets: list[DatasetRequest]
    output_jsonl: Path
    output_json: Path
    images_root: Path
    chinese_ratio: float = 1.0
    seed: int = 42
    backend_config: Path | None = None
    failed_path: Path | None = None
    rejected_path: Path | None = None
    report_path: Path | None = None
    sample_prompt_file: Path | None = None
    utterance_prompt_file: Path | None = None
    limit_per_dataset: int | None = None
    resume: bool = True
    emit_raw: bool = True
    mysql: MysqlConfig | None = None
    batch_id: str | None = None


def load_backend_config(path: Path | str) -> BackendPoolConfig:
    data = _load_json(path)
    backends = [
        BackendSpec(
            name=str(item["name"]),
            api_base=str(item["api_base"]),
            model=str(item["model"]),
            api_key=str(item.get("api_key", "sk-local")),
            concurrency=max(1, int(item.get("concurrency", 1))),
            weight=float(item.get("weight", 1)),
            extra_body=dict(item["extra_body"]) if item.get("extra_body") else None,
        )
        for item in data.get("backends", [])
    ]
    if not backends:
        raise ValueError("backend config must include at least one backend")
    return BackendPoolConfig(
        backends=backends,
        request_timeout=int(data.get("request_timeout", 120)),
        max_retries=int(data.get("max_retries", 2)),
        disable_backend_after_failures=int(data.get("disable_backend_after_failures", 20)),
        fallback_budget_seconds=int(data.get("fallback_budget_seconds", 300)),
        fallback_max_turns=int(data.get("fallback_max_turns", 12)),
    )


def load_translate_json_config(path: Path | str) -> TranslateJsonConfig:
    data = _load_json(path)
    output_jsonl = Path(data["output_jsonl"])
    output_json = Path(data["output_json"])
    return TranslateJsonConfig(
        input=Path(data["input"]),
        output_jsonl=output_jsonl,
        output_json=output_json,
        images_root=Path(data["images_root"]),
        backend_config=_optional_path(data.get("backend_config")),
        done_path=_optional_path(data.get("done_path")) or output_jsonl.with_name(output_jsonl.stem + ".done.jsonl"),
        failed_path=_optional_path(data.get("failed_path")) or output_jsonl.with_name(output_jsonl.stem + ".failed.jsonl"),
        sample_prompt_file=_optional_path(data.get("sample_prompt_file")),
        utterance_prompt_file=_optional_path(data.get("utterance_prompt_file")),
        resume=bool(data.get("resume", True)),
    )


def load_zip_task_config(path: Path | str) -> ZipTaskConfig:
    data = _load_json(path)
    output_jsonl = Path(data["output_jsonl"])
    return ZipTaskConfig(
        dataset_registry=Path(data["dataset_registry"]),
        datasets=[_parse_dataset_request(item) for item in data.get("datasets", ["*"])],
        output_jsonl=output_jsonl,
        output_json=_optional_path(data.get("output_json")) or output_jsonl.with_name(output_jsonl.stem + ".json"),
        images_root=_optional_path(data.get("images_root")) or (output_jsonl.parent / "images"),
        chinese_ratio=float(data.get("chinese_ratio", 1.0)),
        seed=int(data.get("seed", 42)),
        backend_config=_optional_path(data.get("backend_config")),
        failed_path=_optional_path(data.get("failed_path")) or output_jsonl.with_name("failed.jsonl"),
        rejected_path=_optional_path(data.get("rejected_path")) or output_jsonl.with_name("rejected.jsonl"),
        report_path=_optional_path(data.get("report_path")) or output_jsonl.with_name("report.json"),
        sample_prompt_file=_optional_path(data.get("sample_prompt_file")),
        utterance_prompt_file=_optional_path(data.get("utterance_prompt_file")),
        limit_per_dataset=int(data["limit_per_dataset"]) if data.get("limit_per_dataset") is not None else None,
        emit_raw=bool(data.get("emit_raw", True)),
        resume=bool(data.get("resume", True)),
        mysql=load_mysql_config(data.get("mysql")),
        batch_id=str(data["batch_id"]) if data.get("batch_id") else None,
    )


def _parse_dataset_request(item: Any) -> DatasetRequest:
    if isinstance(item, dict):
        return DatasetRequest(
            name=str(item["name"]),
            chinese_ratio=float(item["chinese_ratio"]) if item.get("chinese_ratio") is not None else None,
            limit=int(item["limit"]) if item.get("limit") is not None else None,
        )
    return DatasetRequest(name=str(item))


def _load_json(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def _optional_path(value: Any) -> Path | None:
    return Path(str(value)) if value else None
