from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from typing import Any

from .models import ParseResult, SourceSample, SourceTurn


ROLE_MAP = {
    "human": "human",
    "user": "human",
    "question": "human",
    "gpt": "gpt",
    "assistant": "gpt",
    "answer": "gpt",
}

TEXT_FIELDS = ("texts", "messages", "conversations")
CAPTION_FIELDS = ("caption", "description", "text")
# Columns that pair one question with one answer, in preference order. Only a
# complete pair counts: half of an exchange is not a sample.
QA_FIELDS = (("question", "answer"), ("prompt", "response"))


def parse_row(
    row: Mapping[str, Any],
    source_id: str,
    caption_prompt: str = "请描述这张图片。",
) -> ParseResult:
    images = _extract_images(row)
    if not images:
        return ParseResult(False, reason="missing_image", metadata={"image_count": 0})

    turns = _extract_turns(row)
    if not turns:
        turns = _extract_qa(row)
    if not turns:
        caption = _extract_caption(row)
        if caption:
            turns = [SourceTurn("human", caption_prompt), SourceTurn("gpt", caption)]

    if not turns:
        return ParseResult(False, reason="missing_text", metadata={"image_count": len(images)})

    sample = SourceSample(id=source_id, image_bytes_list=images, turns=turns)
    return ParseResult(True, sample=sample)


def _extract_images(row: Mapping[str, Any]) -> list[bytes]:
    value = row.get("images", row.get("image"))
    if value is None:
        return []
    single = _image_to_bytes(value)
    if single is not None:
        return [single]
    if isinstance(value, list):
        images = []
        for item in value:
            data = _image_to_bytes(item)
            if data is not None:
                images.append(data)
        return images
    return []


def _image_to_bytes(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, Mapping):
        data = value.get("bytes") or value.get("data")
        return data if isinstance(data, bytes) else None
    if hasattr(value, "save"):
        buffer = BytesIO()
        value.save(buffer, format="JPEG")
        return buffer.getvalue()
    return None


def _extract_turns(row: Mapping[str, Any]) -> list[SourceTurn]:
    for field in TEXT_FIELDS:
        value = row.get(field)
        if isinstance(value, list):
            turns: list[SourceTurn] = []
            for item in value:
                turns.extend(_message_to_turns(item))
            return turns
    return []


def _extract_qa(row: Mapping[str, Any]) -> list[SourceTurn]:
    """A question column paired with an answer column, as one exchange.

    Plenty of sets keep the two sides in their own columns rather than in a
    conversation list — RefCOCO ships ``question`` and ``answer`` beside the
    image. Without this they parse as missing text and the whole dataset is
    rejected, even though the exchange is right there.
    """

    for question_field, answer_field in QA_FIELDS:
        question = _field_text(row.get(question_field))
        answer = _field_text(row.get(answer_field))
        if question and answer:
            return [SourceTurn("human", question), SourceTurn("gpt", answer)]
    return []


def _message_to_turns(message: Any) -> list[SourceTurn]:
    if not isinstance(message, Mapping):
        return []
    if "user" in message or "assistant" in message:
        turns = []
        user_text = _field_text(message.get("user"))
        assistant_text = _field_text(message.get("assistant"))
        if user_text:
            turns.append(SourceTurn("human", user_text))
        if assistant_text:
            turns.append(SourceTurn("gpt", assistant_text))
        return turns
    raw_role = message.get("from", message.get("role"))
    role = ROLE_MAP.get(str(raw_role).lower()) if raw_role is not None else None
    if role is None:
        return []
    text = _message_text(message)
    if not text:
        return []
    return [SourceTurn(role, text)]


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", message.get("value", ""))
    return _field_text(content)


def _field_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, Mapping):
                text = part.get("text")
                if text:
                    parts.append(str(text))
            elif part is not None:
                parts.append(str(part))
        return "\n".join(parts).strip()
    return str(content).strip() if content is not None else ""


def _extract_caption(row: Mapping[str, Any]) -> str:
    """First usable caption, whether the column holds one or several.

    Caption sets routinely ship several descriptions per image — Flickr30k
    carries five in a list — and reading only the ``str`` case rejects every
    row of such a dataset for missing text. One caption is enough to build the
    sample, so the first non-empty one wins rather than gluing them together
    into an answer no annotator wrote.
    """

    for field in CAPTION_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""
