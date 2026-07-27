from finevision_to_sharegpt.models import SourceSample, SourceTurn
from finevision_to_sharegpt.translator import build_sharegpt_record, translate_sample


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def chat(self, prompt, image_bytes, timeout=120):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def source_sample():
    return SourceSample(
        id="okvqa:0",
        image_bytes=b"image",
        turns=[
            SourceTurn("human", "What is shown?"),
            SourceTurn("gpt", "A chart."),
        ],
    )


def test_translate_sample_uses_full_sample_json_response():
    client = FakeClient(
        [
            '{"conversations":[{"from":"human","value":"显示了什么？"},{"from":"gpt","value":"一张图表。"}]}'
        ]
    )

    result = translate_sample(client, source_sample(), ["images/a.jpg"])

    assert result.ok
    assert result.record == {
        "id": "okvqa:0",
        "images": ["images/a.jpg"],
        "conversations": [
            {"from": "human", "value": "<image>\n显示了什么？"},
            {"from": "gpt", "value": "一张图表。"},
        ],
    }
    assert "<image>" not in client.prompts[0]


def test_translate_sample_falls_back_per_turn_when_json_is_malformed():
    client = FakeClient(["not json", "显示了什么？", "一张图表。"])

    result = translate_sample(client, source_sample(), ["images/a.jpg"])

    assert result.ok
    assert result.record["conversations"] == [
        {"from": "human", "value": "<image>\n显示了什么？"},
        {"from": "gpt", "value": "一张图表。"},
    ]
    assert len(client.prompts) == 3


def test_translate_sample_trims_extra_trailing_model_turn_without_fallback():
    client = FakeClient(
        [
            (
                '{"conversations":['
                '{"from":"human","value":"显示了什么？"},'
                '{"from":"gpt","value":"一张图表。"},'
                '{"from":"gpt","value":"额外说明。"}'
                "]}"
            )
        ]
    )

    result = translate_sample(client, source_sample(), ["images/a.jpg"])

    assert result.ok
    assert result.record["conversations"] == [
        {"from": "human", "value": "<image>\n显示了什么？"},
        {"from": "gpt", "value": "一张图表。"},
    ]
    assert len(client.prompts) == 1


def test_translate_sample_reports_failure_when_fallback_fails():
    client = FakeClient(["not json", RuntimeError("model unavailable")])

    result = translate_sample(client, source_sample(), ["images/a.jpg"])

    assert not result.ok
    assert result.record is None
    assert result.error == "model unavailable"


def test_build_sharegpt_record_keeps_english_and_inserts_single_image_token():
    sample = SourceSample(
        id="sample:0",
        image_bytes=b"image",
        turns=[
            SourceTurn("human", "<image>\nWhat is shown?"),
            SourceTurn("gpt", "A chart."),
        ],
    )

    record = build_sharegpt_record(sample, ["images/a.jpg"])

    assert record == {
        "id": "sample:0",
        "images": ["images/a.jpg"],
        "conversations": [
            {"from": "human", "value": "<image>\nWhat is shown?"},
            {"from": "gpt", "value": "A chart."},
        ],
    }


def test_build_sharegpt_record_inserts_one_image_token_per_image():
    sample = SourceSample(
        id="sample:multi",
        image_bytes_list=[b"first image", b"second image"],
        turns=[
            SourceTurn("human", "Compare these images."),
            SourceTurn("gpt", "They show different charts."),
        ],
    )

    record = build_sharegpt_record(sample, ["images/a.jpg", "images/b.jpg"])

    assert record == {
        "id": "sample:multi",
        "images": ["images/a.jpg", "images/b.jpg"],
        "conversations": [
            {"from": "human", "value": "<image>\n<image>\nCompare these images."},
            {"from": "gpt", "value": "They show different charts."},
        ],
    }
