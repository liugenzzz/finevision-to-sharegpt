from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
