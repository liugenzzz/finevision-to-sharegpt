import json

import finevision_to_sharegpt.json_io as json_io
from finevision_to_sharegpt.json_io import (
    append_jsonl,
    iter_json_records,
    jsonl_to_json_array,
    load_completed_ids,
    load_records_by_id,
    truncate_file,
)


def test_append_jsonl_and_iter_json_records_stream_jsonl(tmp_path):
    path = tmp_path / "records.jsonl"

    append_jsonl(path, {"id": "one", "value": "第一"})
    append_jsonl(path, {"id": "two", "value": "第二"})

    assert list(iter_json_records(path)) == [
        {"id": "one", "value": "第一"},
        {"id": "two", "value": "第二"},
    ]


def test_iter_json_records_reads_json_array(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps([{"id": "one"}, {"id": "two"}]), encoding="utf-8")

    assert list(iter_json_records(path)) == [{"id": "one"}, {"id": "two"}]


def test_jsonl_to_json_array_converts_without_changing_records(tmp_path):
    jsonl_path = tmp_path / "records.jsonl"
    json_path = tmp_path / "records.json"
    jsonl_path.write_text('{"id":"one"}\n{"id":"two"}\n', encoding="utf-8")

    written = jsonl_to_json_array(jsonl_path, json_path)

    assert written == 2
    assert json.loads(json_path.read_text(encoding="utf-8")) == [{"id": "one"}, {"id": "two"}]


def test_load_records_by_id_and_completed_ids_scan_json_and_jsonl(tmp_path):
    json_path = tmp_path / "records.json"
    jsonl_path = tmp_path / "records.jsonl"
    json_path.write_text(json.dumps([{"id": "one"}, {"id": "two"}]), encoding="utf-8")
    jsonl_path.write_text('{"id":"three"}\n{"id":"four"}\n', encoding="utf-8")

    assert load_records_by_id(json_path) == {"one": {"id": "one"}, "two": {"id": "two"}}
    assert load_completed_ids([json_path, jsonl_path]) == {"one", "two", "three", "four"}


def test_truncate_file_creates_empty_parent_file(tmp_path):
    path = tmp_path / "nested" / "records.jsonl"

    truncate_file(path)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""


def test_merge_jsonl_files_stably_deduplicates_ids_and_writes_json(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text(
        '{"id":"one","value":"first"}\n{"value":"no-id-a"}\n',
        encoding="utf-8",
    )
    second.write_text(
        '{"id":"one","value":"later"}\n{"id":"two"}\n{"value":"no-id-b"}\n',
        encoding="utf-8",
    )

    stats = json_io.merge_jsonl_files([first, second], output)

    expected = [
        {"id": "one", "value": "first"},
        {"value": "no-id-a"},
        {"id": "two"},
        {"value": "no-id-b"},
    ]
    assert list(iter_json_records(output)) == expected
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == expected
    assert stats == {"read": 5, "written": 4, "duplicates": 1}
