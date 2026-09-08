from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ContextOverflow(ValueError):
    """请求的 token 数超过了实例的 max_model_len，服务端 400 拒收。

    单独立一个类型是因为它和别的失败**处理方式完全不同**：超时可以重试、
    解析失败可以回退，而这条样本无论重试多少次、换哪个后端都一样超——
    唯一的出路是把它拆小或者放弃。
    """


class TruncatedResponse(ValueError):
    """服务端 finish_reason=length：话没说完就到顶了。

    这个最阴——HTTP 200，content 里是一段**看起来正常但缺了后半截**的 JSON。
    解析失败后只会报"不是合法 JSON"，没人知道真正的原因是窗口不够写下译文。
    翻译任务的输出长度约等于输入，所以提示词占到窗口一半就会踩到它。
    """


@dataclass(frozen=True)
class SourceTurn:
    role: str
    text: str


@dataclass(frozen=True, init=False)
class SourceSample:
    id: str
    image_bytes_list: list[bytes]
    turns: list[SourceTurn]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        id: str,
        image_bytes_list: list[bytes] | None = None,
        turns: list[SourceTurn] | None = None,
        metadata: dict[str, Any] | None = None,
        image_bytes: bytes | None = None,
    ) -> None:
        if image_bytes_list is None:
            if image_bytes is None:
                raise TypeError("SourceSample requires image_bytes_list or image_bytes")
            image_bytes_list = [image_bytes]
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "image_bytes_list", list(image_bytes_list))
        object.__setattr__(self, "turns", list(turns or []))
        object.__setattr__(self, "metadata", dict(metadata or {}))

    @property
    def image_bytes(self) -> bytes:
        return self.image_bytes_list[0]


@dataclass(frozen=True)
class ParseResult:
    accepted: bool
    sample: SourceSample | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TranslationResult:
    ok: bool
    record: dict[str, Any] | None = None
    error: str | None = None
