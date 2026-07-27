from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


def append_jsonl(path: Path | str, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def truncate_file(path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def iter_json_records(path: Path | str) -> Iterator[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        first = _first_nonspace(handle)
        if not first:
            return
        handle.seek(0)
        if first == "[":
            yield from _iter_json_array(handle)
            return
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL records must be objects at line {line_no}")
            yield record


def load_records_by_id(path: Path | str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in iter_json_records(path):
        record_id = record.get("id")
        if record_id is not None:
            records[str(record_id)] = record
    return records


def load_completed_ids(paths: Iterable[Path | str]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        for record in iter_json_records(path):
            record_id = record.get("id")
            if record_id is not None:
                ids.add(str(record_id))
    return ids


def backfill_jsonl(target: Path | str, sources: Iterable[Path | str]) -> int:
    """Append records from ``sources`` whose id is missing from ``target``.

    Used on resume so ``target`` (the jsonl source of truth) contains every
    record that earlier runs may have written only to e.g. the json array or
    the done file. Returns the number of records appended.
    """
    target = Path(target)
    existing = {
        str(record["id"])
        for record in iter_json_records(target)
        if record.get("id") is not None
    }
    added = 0
    for source in sources:
        source = Path(source)
        if source == target:
            continue
        for record in iter_json_records(source):
            record_id = record.get("id")
            if record_id is None:
                continue
            record_id = str(record_id)
            if record_id in existing:
                continue
            append_jsonl(target, record)
            existing.add(record_id)
            added += 1
    return added


def prune_failed(failed_path: Path | str, succeeded_ids: set[str]) -> int:
    """Rewrite the failed log so it stays compact across resumes.

    Keeps only the latest failure per id, and drops any id present in
    ``succeeded_ids`` (those have since been translated successfully).
    Records without an id are preserved as-is. Returns the kept count.
    """
    failed_path = Path(failed_path)
    if not failed_path.exists():
        return 0
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    extras: list[dict[str, Any]] = []
    for record in iter_json_records(failed_path):
        record_id = record.get("id")
        if record_id is None:
            extras.append(record)
            continue
        record_id = str(record_id)
        if record_id in succeeded_ids:
            continue
        if record_id not in latest:
            order.append(record_id)
        latest[record_id] = record
    truncate_file(failed_path)
    for record_id in order:
        append_jsonl(failed_path, latest[record_id])
    for record in extras:
        append_jsonl(failed_path, record)
    return len(order) + len(extras)


def jsonl_to_json_array(input_path: Path | str, output_path: Path | str) -> int:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("[\n")
        for record in iter_json_records(input_path):
            if count:
                handle.write(",\n")
            text = json.dumps(record, ensure_ascii=False, indent=2)
            handle.write("  " + text.replace("\n", "\n  "))
            count += 1
        handle.write("\n]\n")
    return count


def merge_jsonl_files(inputs: list[Path], output: Path) -> dict[str, int]:
    output = Path(output)
    truncate_file(output)
    seen_ids: set[str] = set()
    stats = {"read": 0, "written": 0, "duplicates": 0}
    for input_path in inputs:
        for record in iter_json_records(input_path):
            stats["read"] += 1
            record_id = record.get("id")
            if record_id is not None:
                normalized_id = str(record_id)
                if normalized_id in seen_ids:
                    stats["duplicates"] += 1
                    continue
                seen_ids.add(normalized_id)
            append_jsonl(output, record)
            stats["written"] += 1
    jsonl_to_json_array(output, output.with_suffix(".json"))
    return stats


def _first_nonspace(handle: Any) -> str:
    while True:
        char = handle.read(1)
        if not char:
            return ""
        if not char.isspace():
            return char


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
