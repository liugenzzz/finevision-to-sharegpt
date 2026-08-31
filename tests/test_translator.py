import pytest

from finevision_to_sharegpt.models import SourceSample, SourceTurn
from finevision_to_sharegpt.translator import (
    build_sharegpt_record,
    extract_json_object,
    strip_reasoning,
    translate_sample,
)


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


# -- reasoning models -------------------------------------------------------

_TRANSLATED = '{"conversations":[{"from":"human","value":"这是什么？"},{"from":"gpt","value":"一张图表。"}]}'


def _thinking_sample():
    return SourceSample(
        id="t:1",
        image_bytes_list=[b"\xff\xd8\xffx"],
        turns=[SourceTurn("human", "What is this?"), SourceTurn("gpt", "A chart.")],
    )


class _CountingClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, prompt, image_bytes, timeout=120):
        self.calls += 1
        return self.response


def test_strip_reasoning_removes_a_complete_think_block():
    assert strip_reasoning("<think>weighing it up</think>\nanswer") == "answer"


def test_strip_reasoning_handles_a_pre_opened_tag():
    # Many chat templates emit the opening tag themselves, so only the close
    # comes back from the model.
    assert strip_reasoning("reasoning here\n</think>\nanswer") == "answer"


def test_strip_reasoning_drops_an_unterminated_block():
    # Cut off mid-reasoning by max_tokens: nothing usable follows.
    assert strip_reasoning("answer so far<think>still thinking") == "answer so far"


def test_strip_reasoning_leaves_plain_text_alone():
    assert strip_reasoning("  just an answer  ") == "just an answer"


def test_extract_json_object_unwraps_fences_and_prose():
    assert extract_json_object("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json_object("Here you go:\n{\"a\": 1}\nhope that helps") == {"a": 1}
    assert extract_json_object("<think>x</think>{\"a\": 1}") == {"a": 1}


def test_extract_json_object_reports_a_reply_with_no_object():
    with pytest.raises(ValueError, match="did not contain a JSON object"):
        extract_json_object("<think>only reasoning</think>")


@pytest.mark.parametrize(
    "response",
    [
        "<think>\nreasoning\n</think>\n" + _TRANSLATED,
        "reasoning\n</think>\n" + _TRANSLATED,
        "<think>x</think>\n```json\n" + _TRANSLATED + "\n```",
        "<think>x</think>\nHere is the result:\n" + _TRANSLATED + "\nHope this helps!",
        _TRANSLATED,
    ],
)
def test_reasoning_replies_parse_in_a_single_call(response):
    client = _CountingClient(response)

    result = translate_sample(client, _thinking_sample(), ["images/a.jpg"])

    values = [turn["value"].replace("<image>\n", "") for turn in result.record["conversations"]]
    assert result.ok
    # One call: a parse failure would fall back to per-utterance translation,
    # tripling the cost and storing the reasoning text as the translation.
    assert client.calls == 1
    assert values == ["这是什么？", "一张图表。"]


def test_a_reply_that_is_only_reasoning_fails_instead_of_storing_it():
    client = _CountingClient("<think>thinking forever</think>")

    result = translate_sample(client, _thinking_sample(), ["images/a.jpg"])

    assert not result.ok
    assert "reasoning" in (result.error or "")
