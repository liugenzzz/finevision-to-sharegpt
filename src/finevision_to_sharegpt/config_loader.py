from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackendSpec:
    name: str
    api_base: str
    model: str
    api_key: str
    concurrency: int
    weight: float = 1.0


@dataclass(frozen=True)
class BackendPoolConfig:
    backends: list[BackendSpec]
    request_timeout: int = 120
    max_retries: int = 2
    disable_backend_after_failures: int = 20


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
