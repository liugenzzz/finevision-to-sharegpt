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
    # 两段原因都要留下：整段翻译为什么被拒，以及回退为什么也没成。
    assert result.error == "not_json -> fallback: model unavailable"


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


# -- 回退的两道上限 ----------------------------------------------------------


class SlowClient:
    """每次调用都推进一个假时钟，用来在不真等的前提下逼出预算耗尽。"""

    def __init__(self, first, seconds_per_call=120):
        self.responses = [first]
        self.seconds_per_call = seconds_per_call
        self.calls = 0
        self.timeouts = []
        self.now = 0.0

    def chat(self, prompt, image_bytes, timeout=120):
        self.calls += 1
        self.timeouts.append(timeout)
        self.now += self.seconds_per_call
        if self.responses:
            return self.responses.pop(0)
        return "一句译文。"


def long_sample(turns):
    return SourceSample(
        id="long",
        image_bytes_list=[b"img"],
        turns=[SourceTurn("human" if i % 2 == 0 else "gpt", f"line {i}") for i in range(turns)],
    )


def test_a_conversation_longer_than_the_cap_never_enters_the_fallback(monkeypatch):
    # 39 轮的对话逐句翻要占住线程好几个小时，代价远超这条样本的价值。
    client = SlowClient("not json")

    result = translate_sample(client, long_sample(39), ["a.jpg"], fallback_max_turns=12)

    assert not result.ok
    assert "exceeds the fallback cap" in result.error
    assert client.calls == 1          # 只有整段那一次，一句都没退化


def test_the_fallback_stops_when_the_total_budget_runs_out(monkeypatch):
    client = SlowClient("not json", seconds_per_call=120)
    monkeypatch.setattr("finevision_to_sharegpt.translator.time.monotonic", lambda: client.now)

    result = translate_sample(
        client, long_sample(10), ["a.jpg"],
        timeout=120, fallback_budget_seconds=300, fallback_max_turns=12,
    )

    assert not result.ok
    assert "budget of 300s ran out" in result.error
    # 整段 1 次 + 预算内的几句，远少于 10 句。
    assert 1 < client.calls < 1 + 10


def test_the_last_call_gets_only_the_time_that_is_left(monkeypatch):
    client = SlowClient("not json", seconds_per_call=100)
    monkeypatch.setattr("finevision_to_sharegpt.translator.time.monotonic", lambda: client.now)

    translate_sample(
        client, long_sample(6), ["a.jpg"],
        timeout=120, fallback_budget_seconds=250, fallback_max_turns=12,
    )

    # 首次整段调用拿满 timeout；之后每句只拿剩余预算，且递减。
    fallback_timeouts = client.timeouts[1:]
    assert fallback_timeouts == sorted(fallback_timeouts, reverse=True)
    assert max(fallback_timeouts) <= 250


def test_a_short_conversation_still_completes_through_the_fallback():
    client = FakeClient(["not json", "第一句。", "第二句。"])

    result = translate_sample(client, source_sample(), ["a.jpg"], fallback_budget_seconds=300)

    assert result.ok
    assert [t["value"] for t in result.record["conversations"]] == ["<image>\n第一句。", "第二句。"]


# -- 触发原因分类 ------------------------------------------------------------


def test_the_trigger_reason_is_reported_with_a_groupable_code():
    seen = []
    client = FakeClient(["not json", "一句。", "两句。"])

    translate_sample(client, source_sample(), ["a.jpg"], on_fallback=lambda code, turns: seen.append((code, turns)))

    assert seen == [("not_json", 2)]


def test_a_truncated_turn_list_is_told_apart_from_bad_json():
    seen = []
    client = FakeClient(['{"conversations": [{"from": "human", "value": "只有一轮"}]}', "一句。", "两句。"])

    translate_sample(client, source_sample(), ["a.jpg"], on_fallback=lambda code, turns: seen.append(code))

    assert seen == ["too_few_turns"]
