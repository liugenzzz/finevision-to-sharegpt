from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .json_io import (
    append_jsonl,
    backfill_jsonl,
    jsonl_to_json_array,
    load_completed_ids,
    prune_failed,
    truncate_file,
)
from .models import SourceSample, SourceTurn


@dataclass(frozen=True)
class TranslationTask:
    id: str
    sample: SourceSample
    image_paths: list[str]
    metadata: dict[str, Any] | None = None


def record_to_source_sample(record: dict[str, Any], images_root: Path | str) -> SourceSample:
    image_paths = _as_list(record.get("images", record.get("image")))
    if not image_paths:
        raise ValueError("record did not include images")
    root = Path(images_root)
    image_bytes_list = []
    for item in image_paths:
        image_path = Path(str(item))
        absolute_path = image_path if image_path.is_absolute() else root / image_path
        image_bytes_list.append(absolute_path.read_bytes())

    turns = []
    for message in record.get("conversations", record.get("messages", [])):
        if not isinstance(message, dict):
            continue
        role = message.get("from", message.get("role"))
        if role == "user":
            role = "human"
        elif role == "assistant":
            role = "gpt"
        value = message.get("value", message.get("content"))
        text = _message_text(value)
        if role in {"human", "gpt"} and text:
            turns.append(SourceTurn(str(role), _strip_image_tokens(text)))
    if not turns:
        raise ValueError("record did not include conversations")
    return SourceSample(
        id=str(record.get("id", Path(str(image_paths[0])).stem)),
        image_bytes_list=image_bytes_list,
        turns=turns,
    )


def run_translation_job(
    tasks: Iterable[TranslationTask],
    output_jsonl: Path | str,
    output_json: Path | str,
    done_path: Path | str,
    failed_path: Path | str,
    translate: Callable[[TranslationTask], dict[str, Any]],
    resume: bool = True,
) -> dict[str, int]:
    output_jsonl = Path(output_jsonl)
    output_json = Path(output_json)
    done_path = Path(done_path)
    failed_path = Path(failed_path)
    if not resume:
        truncate_file(output_jsonl)
        truncate_file(done_path)
        truncate_file(failed_path)

    if resume:
        backfill_jsonl(output_jsonl, [done_path, output_json])

    completed = load_completed_ids([done_path, output_jsonl, output_json]) if resume else set()
    stats = {"processed": 0, "written": 0, "failed": 0, "skipped": 0}

    for task in tasks:
        if task.id in completed:
            stats["skipped"] += 1
            continue
        stats["processed"] += 1
        try:
            record = translate(task)
        except Exception as exc:
            append_jsonl(failed_path, {"id": task.id, "error": str(exc), **(task.metadata or {})})
            stats["failed"] += 1
            continue
        append_jsonl(output_jsonl, record)
        append_jsonl(done_path, record)
        completed.add(task.id)
        stats["written"] += 1

    jsonl_to_json_array(output_jsonl, output_json)
    prune_failed(failed_path, load_completed_ids([output_jsonl, done_path]))
    return stats


def run_translation_job_with_backend_pool(
    tasks: Iterable[TranslationTask],
    output_jsonl: Path | str,
    output_json: Path | str,
    done_path: Path | str,
    failed_path: Path | str,
    backend_pool: Any,
    handler: Callable[[TranslationTask, Any, int], dict[str, Any]],
    resume: bool = True,
    progress: Any | None = None,
) -> dict[str, int]:
    output_jsonl = Path(output_jsonl)
    output_json = Path(output_json)
    done_path = Path(done_path)
    failed_path = Path(failed_path)
    if not resume:
        truncate_file(output_jsonl)
        truncate_file(done_path)
        truncate_file(failed_path)

    if resume:
        backfill_jsonl(output_jsonl, [done_path, output_json])

    completed = load_completed_ids([done_path, output_jsonl, output_json]) if resume else set()
    stats = {"processed": 0, "written": 0, "failed": 0, "skipped": 0}

    def pending_tasks() -> Iterable[TranslationTask]:
        for task in tasks:
            if task.id in completed:
                stats["skipped"] += 1
                _update_progress(progress, stats)
                continue
            yield task

    for result in backend_pool.map_unordered(pending_tasks(), handler):
        task = result.item
        stats["processed"] += 1
        if not result.ok:
            append_jsonl(
                failed_path,
                {
                    "id": task.id,
                    "error": result.error,
                    "backend": result.backend_name,
                    **(task.metadata or {}),
                },
            )
            stats["failed"] += 1
            _update_progress(progress, stats)
            continue
        append_jsonl(output_jsonl, result.value)
        append_jsonl(done_path, result.value)
        stats["written"] += 1
        _update_progress(progress, stats)

    jsonl_to_json_array(output_jsonl, output_json)
    prune_failed(failed_path, load_completed_ids([output_jsonl, done_path]))
    return stats


def _update_progress(progress: Any | None, stats: dict[str, int]) -> None:
    if progress is None:
        return
    if hasattr(progress, "update"):
        progress.update(1)
    if hasattr(progress, "set_postfix"):
        progress.set_postfix(
            processed=stats.get("processed", 0),
            written=stats.get("written", 0),
            failed=stats.get("failed", 0),
            skipped=stats.get("skipped", 0),
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(value).strip() if value is not None else ""


def _strip_image_tokens(text: str) -> str:
    return text.replace("<image>", "").lstrip()
