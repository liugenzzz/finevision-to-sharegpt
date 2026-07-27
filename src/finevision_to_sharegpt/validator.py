from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import ValidationResult


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def get_messages(item: dict[str, Any]) -> list[Any]:
    if isinstance(item.get("messages"), list):
        return item["messages"]
    if isinstance(item.get("conversations"), list):
        return item["conversations"]
    return []


def get_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", message.get("value", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image":
                    parts.append("<image>")
                elif part.get("type") == "video":
                    parts.append("<video>")
                else:
                    parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def count_tokens(item: dict[str, Any], token: str) -> int:
    return sum(get_text(message).count(token) for message in get_messages(item))


def validate_record(item: dict[str, Any]) -> ValidationResult:
    messages = get_messages(item)
    image_tokens = count_tokens(item, "<image>")
    video_tokens = count_tokens(item, "<video>")
    images = as_list(item.get("images", item.get("image")))
    videos = as_list(item.get("videos", item.get("video")))

    reasons = []
    if not messages:
        reasons.append("messages_empty")
    if image_tokens != len(images):
        reasons.append(f"image_token_count={image_tokens} image_field_count={len(images)}")
    if video_tokens != len(videos):
        reasons.append(f"video_token_count={video_tokens} video_field_count={len(videos)}")
    return ValidationResult(ok=not reasons, reasons=reasons)


def load_records(path: Path | str) -> tuple[list[dict[str, Any]], str]:
    path = Path(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return [], "jsonl"
    if text[0] == "[":
        return json.loads(text), "json"

    records = []
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc
    return records, "jsonl"


def iter_records(path: Path | str) -> Iterator[dict[str, Any]]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        first = ""
        while True:
            char = handle.read(1)
            if not char:
                return
            if not char.isspace():
                first = char
                break
        handle.seek(0)
        if first == "[":
            yield from _iter_json_array(handle)
            return
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc


def _iter_json_array(handle: Any, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False
    opened = False
    need_comma = False

    def read_more() -> None:
        nonlocal buffer, eof
        chunk = handle.read(chunk_size)
        if chunk:
            buffer += chunk
        else:
            eof = True

    while True:
        buffer = buffer.lstrip()
        if not buffer:
            if eof:
                return
            read_more()
            continue
        if not opened:
            if buffer[0] != "[":
                raise ValueError("Invalid JSON array: expected '['")
            buffer = buffer[1:]
            opened = True
            continue
        if buffer[0] == "]":
            return
        if need_comma:
            if buffer[0] != ",":
                raise ValueError("Invalid JSON array: expected ','")
            buffer = buffer[1:]
            need_comma = False
            continue
        try:
            record, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            if eof:
                raise
            read_more()
            continue
        if not isinstance(record, dict):
            raise ValueError("JSON array records must be objects")
        yield record
        buffer = buffer[end:]
        need_comma = True


def dump_records(records: list[dict[str, Any]], path: Path | str, fmt: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if fmt == "json":
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        else:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def validate_file(
    input_path: Path | str,
    output_path: Path | str,
    reject_path: Path | str,
) -> tuple[int, int, int]:
    records, fmt = load_records(input_path)
    kept = []
    rejected = []

    for idx, item in enumerate(records):
        result = validate_record(item)
        if result.ok:
            kept.append(item)
            continue
        bad = dict(item)
        bad["_reject_index"] = idx
        bad["_reject_reasons"] = result.reasons
        rejected.append(bad)

    dump_records(kept, output_path, fmt)
    dump_records(rejected, reject_path, "jsonl")
    return len(records), len(kept), len(rejected)
