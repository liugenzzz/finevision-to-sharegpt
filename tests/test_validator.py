import json

from finevision_to_sharegpt.validator import iter_records, load_records, validate_file, validate_record


def test_validate_record_accepts_sharegpt_with_matching_image_token():
    item = {
        "images": ["images/a.jpg"],
        "conversations": [
            {"from": "human", "value": "<image>\n请描述。"},
            {"from": "gpt", "value": "一张图。"},
        ],
    }

    result = validate_record(item)

    assert result.ok
    assert result.reasons == []


def test_validate_record_rejects_image_token_mismatch():
    item = {
        "images": ["images/a.jpg", "images/b.jpg"],
        "conversations": [{"from": "human", "value": "<image>\n请描述。"}],
    }

    result = validate_record(item)

    assert not result.ok
    assert result.reasons == ["image_token_count=1 image_field_count=2"]


def test_validate_record_accepts_matching_multi_image_tokens():
    item = {
        "images": ["images/a.jpg", "images/b.jpg"],
        "conversations": [{"from": "human", "value": "<image>\n<image>\nCompare."}],
    }

    result = validate_record(item)

    assert result.ok


def test_validate_record_accepts_messages_content_format():
    item = {
        "images": ["images/a.jpg"],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<image>\n问题"}]},
            {"role": "assistant", "content": "答案"},
        ],
    }

    assert validate_record(item).ok


def test_validate_record_rejects_empty_messages():
    result = validate_record({"images": ["images/a.jpg"], "conversations": []})

    assert not result.ok
    assert "messages_empty" in result.reasons


def test_load_records_reads_json_array_and_jsonl(tmp_path):
    json_path = tmp_path / "data.json"
    json_path.write_text(json.dumps([{"id": "1"}]), encoding="utf-8")
    jsonl_path = tmp_path / "data.jsonl"
    jsonl_path.write_text('{"id": "1"}\n{"id": "2"}\n', encoding="utf-8")

    assert load_records(json_path) == ([{"id": "1"}], "json")
    assert load_records(jsonl_path) == ([{"id": "1"}, {"id": "2"}], "jsonl")


def test_iter_records_streams_json_array_and_jsonl(tmp_path):
    json_path = tmp_path / "data.json"
    json_path.write_text(json.dumps([{"id": "1"}, {"id": "2"}]), encoding="utf-8")
    jsonl_path = tmp_path / "data.jsonl"
    jsonl_path.write_text('{"id": "3"}\n{"id": "4"}\n', encoding="utf-8")

    assert list(iter_records(json_path)) == [{"id": "1"}, {"id": "2"}]
    assert list(iter_records(jsonl_path)) == [{"id": "3"}, {"id": "4"}]


def test_validate_file_writes_clean_and_reject_files(tmp_path):
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "clean.jsonl"
    rejects_path = tmp_path / "rejects.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "images": ["images/a.jpg"],
                        "conversations": [{"from": "human", "value": "<image>\n问题"}],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "images": ["images/a.jpg", "images/b.jpg"],
                        "conversations": [{"from": "human", "value": "<image>\n问题"}],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    total, kept, rejected = validate_file(input_path, output_path, rejects_path)

    assert (total, kept, rejected) == (2, 1, 1)
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1
    reject = json.loads(rejects_path.read_text(encoding="utf-8").splitlines()[0])
    assert reject["_reject_index"] == 1
    assert reject["_reject_reasons"] == ["image_token_count=1 image_field_count=2"]
