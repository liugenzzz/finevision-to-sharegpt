from __future__ import annotations

import json
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
) -> TranslationResult:
    try:
        response = client.chat(
            prompt=sample_prompt.format(input=_turns_json(sample)),
            image_bytes=sample.image_bytes_list,
            timeout=timeout,
        )
        conversations = _parse_conversations(response, sample)
    except Exception:
        try:
            conversations = _fallback_translate(client, sample, utterance_prompt, timeout)
        except Exception as exc:
            return TranslationResult(ok=False, error=str(exc))

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


def _parse_conversations(response: str, sample: SourceSample) -> list[dict[str, str]]:
    data = json.loads(response)
    conversations = data.get("conversations")
    if not isinstance(conversations, list):
        raise ValueError("model JSON did not include conversations")
    if len(conversations) < len(sample.turns):
        raise ValueError("translated turn count did not match source")
    if len(conversations) > len(sample.turns):
        conversations = conversations[: len(sample.turns)]

    normalized = []
    for translated, source_turn in zip(conversations, sample.turns):
        if not isinstance(translated, dict):
            raise ValueError("translated turn was not an object")
        role = translated.get("from")
        value = translated.get("value")
        if role != source_turn.role:
            raise ValueError("translated role order did not match source")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("translated value was empty")
        normalized.append({"from": role, "value": _strip_image_token(value.strip())})
    return normalized


def _fallback_translate(
    client: Any,
    sample: SourceSample,
    utterance_prompt: str,
    timeout: int,
) -> list[dict[str, str]]:
    conversations = []
    for turn in sample.turns:
        translated = client.chat(
            prompt=utterance_prompt.format(input=turn.text),
            image_bytes=sample.image_bytes_list,
            timeout=timeout,
        )
        conversations.append({"from": turn.role, "value": _strip_image_token(translated.strip())})
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
