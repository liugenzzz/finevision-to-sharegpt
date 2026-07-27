from finevision_to_sharegpt.sample_parser import parse_row


class FakePILImage:
    def save(self, handle, format):
        assert format == "JPEG"
        handle.write(b"\xff\xd8\xffpil image\xff\xd9")


def test_parse_row_normalizes_single_image_messages():
    row = {
        "images": [b"image bytes"],
        "messages": [
            {"role": "user", "content": "What is shown?"},
            {"role": "assistant", "content": "A chart."},
        ],
    }

    parsed = parse_row(row, source_id="okvqa:part.parquet:1")

    assert parsed.accepted
    assert parsed.sample.id == "okvqa:part.parquet:1"
    assert parsed.sample.image_bytes == b"image bytes"
    assert [(turn.role, turn.text) for turn in parsed.sample.turns] == [
        ("human", "What is shown?"),
        ("gpt", "A chart."),
    ]


def test_parse_row_converts_pil_like_images_to_bytes():
    row = {"images": [FakePILImage()], "caption": "A captcha."}

    parsed = parse_row(row, source_id="pil:0")

    assert parsed.accepted
    assert parsed.sample.image_bytes == b"\xff\xd8\xffpil image\xff\xd9"


def test_parse_row_accepts_texts_with_from_value_fields():
    row = {
        "image": b"image bytes",
        "texts": [
            {"from": "human", "value": "Read the text."},
            {"from": "gpt", "value": "The title is Sales."},
        ],
    }

    parsed = parse_row(row, source_id="cocotext:0")

    assert parsed.accepted
    assert [(turn.role, turn.text) for turn in parsed.sample.turns] == [
        ("human", "Read the text."),
        ("gpt", "The title is Sales."),
    ]


def test_parse_row_accepts_finevision_user_assistant_pairs():
    row = {
        "images": [b"image bytes"],
        "texts": [
            {"user": "What is shown?", "assistant": "A chart."},
            {"user": "How many bars?", "assistant": "Three."},
        ],
    }

    parsed = parse_row(row, source_id="chartqa:0")

    assert parsed.accepted
    assert [(turn.role, turn.text) for turn in parsed.sample.turns] == [
        ("human", "What is shown?"),
        ("gpt", "A chart."),
        ("human", "How many bars?"),
        ("gpt", "Three."),
    ]


def test_parse_row_uses_caption_fallback():
    row = {"image": b"image bytes", "caption": "A person riding a bike."}

    parsed = parse_row(row, source_id="caption:0", caption_prompt="请描述这张图片。")

    assert parsed.accepted
    assert [(turn.role, turn.text) for turn in parsed.sample.turns] == [
        ("human", "请描述这张图片。"),
        ("gpt", "A person riding a bike."),
    ]


def test_parse_row_accepts_multi_image_samples():
    row = {"images": [b"one", b"two"], "caption": "Two images."}

    parsed = parse_row(row, source_id="multi:0")

    assert parsed.accepted
    assert parsed.sample.image_bytes_list == [b"one", b"two"]
    assert parsed.sample.image_bytes == b"one"


def test_parse_row_rejects_rows_without_images():
    parsed = parse_row({"caption": "No image."}, source_id="no-image:0")

    assert not parsed.accepted
    assert parsed.reason == "missing_image"


def test_parse_row_rejects_rows_without_usable_text():
    parsed = parse_row({"image": b"image bytes"}, source_id="no-text:0")

    assert not parsed.accepted
    assert parsed.reason == "missing_text"
