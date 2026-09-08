from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import SourceSample, TranslationResult


DEFAULT_SAMPLE_PROMPT = """你是一名专业的多模态数据集翻译与标注助手。请将 FineVision 数据集样本中的英文对话内容翻译为中文，并输出严格 JSON。随附的图片仅作为辅助理解的上下文。

最重要的原则：
- 这是一个翻译任务，不是看图问答任务。无论你能否完全看懂图片，都必须把每一轮文本完整翻译成中文。
- 即使图片内容模糊、缺失或你无法理解，也绝不能因此跳过翻译、保留英文或输出"无法翻译"之类的说明；此时按字面忠实翻译文本即可。
- 除下列保留规则中明确允许的内容外，输出里不得出现成段的英文原文。

任务要求：
1. 翻译所有英文自然语言文本，保持原始语义准确，不随意增删信息。
2. 在能看懂图片时，结合图片让中文表达更自然准确；看不懂图片时，仅依据文本翻译。
3. 保留专业术语、模型名称、数据集名称、数学符号、代码、公式、单位、选项编号、变量名、字段名、文件路径、URL、特殊标记和图像占位符。
4. 如果原文是选择题，保留选项结构和选项编号。
5. 如果原文包含表格、列表、JSON 片段或代码块，保持结构不变，仅翻译其中的自然语言内容。
6. 不要添加解释、注释、免责声明或额外字段。
7. 必须保持输入对话轮次数量不变。
8. 必须保持角色顺序（from 字段）不变。
9. 输出必须是合法 JSON，且只输出该 JSON，不要包裹代码块或额外文字。

输出 JSON 格式：
{{"conversations":[{{"from":"human","value":"中文内容"}},{{"from":"gpt","value":"中文内容"}}]}}

待翻译对话：
{input}
"""

DEFAULT_UTTERANCE_PROMPT = """你是一名专业翻译助手。请将下面英文内容翻译成中文。

要求：
1. 保持原意，不增删信息。
2. 保留术语、代码、公式、单位、URL、路径、变量名和特殊标记。
3. 只输出翻译后的中文内容，不要解释。

待翻译内容：
{input}
"""


_THINK_PAIR = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's ``<think>`` block, keeping only the answer.

    Qwen3-style deployments emit reasoning before the answer, and many chat
    templates pre-open the tag so only ``</think>`` comes back. Left in, the
    JSON parse fails, the per-utterance fallback fires (three model calls
    instead of one) and the reasoning text itself gets stored as the
    translation while the record still reports success.
    """

    cleaned = _THINK_PAIR.sub("", text)
    closes = list(_THINK_CLOSE.finditer(cleaned))
    if closes:
        # A pre-opened tag leaves a bare close: the answer follows the last one.
        cleaned = cleaned[closes[-1].end():]
    opens = list(_THINK_OPEN.finditer(cleaned))
    if opens:
        # Unterminated block: the response was cut off mid-reasoning.
        cleaned = cleaned[: opens[0].start()]
    return cleaned.strip()


def extract_json_object(text: str) -> Any:
    """Parse the JSON object out of a reply that may carry prose or fences."""

    candidate = _CODE_FENCE.sub("", strip_reasoning(text)).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model response did not contain a JSON object")
    return json.loads(candidate[start : end + 1])


def load_prompt(path: Path | str | None, default: str) -> str:
    if path is None:
        return default
    return Path(path).read_text(encoding="utf-8")


def translate_sample(
    client: Any,
    sample: SourceSample,
    image_paths: list[str],
    sample_prompt: str = DEFAULT_SAMPLE_PROMPT,
    utterance_prompt: str = DEFAULT_UTTERANCE_PROMPT,
    timeout: int = 120,
    fallback_budget_seconds: int = 300,
    fallback_max_turns: int = 12,
    on_fallback: Callable[[str, int], None] | None = None,
) -> TranslationResult:
    """Translate one sample, falling back to per-utterance calls if needed.

    The fallback is bounded twice over. Without a bound it is one call per turn
    at the full request timeout with no cap on turns, so a single long
    conversation can hold a worker for hours: a 39-turn sample at 300s a turn is
    3.27 of them, and a pool loses that slot for the duration. A sample that
    cannot be translated inside the budget is worth far less than the throughput
    spent insisting on it — it fails, lands in ``failed.jsonl``, and can be
    retried later in a run of its own.
    """

    try:
        response = client.chat(
            prompt=sample_prompt.format(input=_turns_json(sample)),
            image_bytes=sample.image_bytes_list,
            timeout=timeout,
        )
        conversations = _parse_conversations(response, sample)
    except Exception as exc:
        if on_fallback is not None:
            on_fallback(failure_code(exc), len(sample.turns))
        try:
            conversations = _fallback_translate(
                client,
                sample,
                utterance_prompt,
                timeout,
                budget_seconds=fallback_budget_seconds,
                max_turns=fallback_max_turns,
            )
        except Exception as fallback_error:
            return TranslationResult(
                ok=False, error=f"{failure_code(exc)} -> fallback: {fallback_error}"
            )

    record = build_sharegpt_record(sample, image_paths, conversations)
    return TranslationResult(ok=True, record=record)


def build_sharegpt_record(
    sample: SourceSample,
    image_paths: list[str],
    conversations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if conversations is None:
        conversations = [{"from": turn.role, "value": turn.text} for turn in sample.turns]
    return {
        "id": sample.id,
        "images": image_paths,
        "conversations": _insert_image_token(conversations, len(image_paths)),
    }


def _turns_json(sample: SourceSample) -> str:
    return json.dumps(
        [{"from": turn.role, "value": turn.text} for turn in sample.turns],
        ensure_ascii=False,
    )


class ParseFailure(ValueError):
    """A whole-sample response the parser rejected, with a groupable ``code``.

    The fallback that follows costs one model call per turn, so the run needs to
    know *why* it is being paid for. A free-text message cannot be counted; a
    short code can, and the codes are what tell "the model is emitting prose"
    apart from "the answer was truncated".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_conversations(response: str, sample: SourceSample) -> list[dict[str, str]]:
    try:
        data = extract_json_object(response)
    except Exception as exc:
        raise ParseFailure("not_json", f"response was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ParseFailure("not_object", "model JSON was not an object")
    conversations = data.get("conversations")
    if not isinstance(conversations, list):
        raise ParseFailure("no_conversations", "model JSON did not include conversations")
    if len(conversations) < len(sample.turns):
        raise ParseFailure(
            "too_few_turns",
            f"translated {len(conversations)} turns for a {len(sample.turns)}-turn sample",
        )
    if len(conversations) > len(sample.turns):
        conversations = conversations[: len(sample.turns)]

    normalized = []
    for translated, source_turn in zip(conversations, sample.turns):
        if not isinstance(translated, dict):
            raise ParseFailure("turn_not_object", "translated turn was not an object")
        role = translated.get("from")
        value = translated.get("value")
        if role != source_turn.role:
            raise ParseFailure("role_order", "translated role order did not match source")
        if not isinstance(value, str) or not value.strip():
            raise ParseFailure("empty_value", "translated value was empty")
        normalized.append({"from": role, "value": _strip_image_token(value.strip())})
    return normalized


def failure_code(error: BaseException) -> str:
    """One short, groupable label for whatever went wrong."""

    if isinstance(error, ParseFailure):
        return error.code
    name = type(error).__name__
    if "Timeout" in name:
        return "timeout"
    return f"other:{name}"


def _fallback_translate(
    client: Any,
    sample: SourceSample,
    utterance_prompt: str,
    timeout: int,
    budget_seconds: int = 300,
    max_turns: int = 12,
) -> list[dict[str, str]]:
    """One call per turn, under a total time budget and a turn cap.

    The turn cap is checked before anything is spent: a 39-turn conversation is
    never worth entering utterance by utterance. The budget then bounds the rest,
    and each call is given only the time that is actually left, so the last one
    cannot overrun it.
    """

    if max_turns and len(sample.turns) > max_turns:
        raise TimeoutError(
            f"{len(sample.turns)} turns exceeds the fallback cap of {max_turns}"
        )
    deadline = time.monotonic() + budget_seconds if budget_seconds else None

    conversations = []
    for index, turn in enumerate(sample.turns):
        call_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"fallback budget of {budget_seconds}s ran out after {index}"
                    f"/{len(sample.turns)} turns"
                )
            call_timeout = max(1, min(timeout, int(remaining)))
        translated = client.chat(
            prompt=utterance_prompt.format(input=turn.text),
            image_bytes=sample.image_bytes_list,
            timeout=call_timeout,
        )
        cleaned = strip_reasoning(translated)
        if not cleaned.strip():
            raise ValueError("model returned only reasoning, no translation")
        conversations.append({"from": turn.role, "value": _strip_image_token(cleaned.strip())})
    return conversations


def _insert_image_token(conversations: list[dict[str, str]], image_count: int = 1) -> list[dict[str, str]]:
    output = [dict(item) for item in conversations]
    for item in output:
        if item["from"] == "human":
            image_prefix = "\n".join("<image>" for _ in range(max(1, image_count)))
            item["value"] = image_prefix + "\n" + _strip_image_token(item["value"])
            break
    return output


def _strip_image_token(text: str) -> str:
    return text.replace("<image>", "").lstrip()
