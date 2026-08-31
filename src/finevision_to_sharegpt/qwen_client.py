from __future__ import annotations

import base64
from typing import Any

import httpx

from .image_store import detect_image_extension


MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class QwenClient:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        http_client: Any | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.http_client = http_client or httpx.Client()
        # Merged into every request body: lets a deployment turn off reasoning
        # (``chat_template_kwargs.enable_thinking``), cap tokens, set
        # temperature, and so on without a code change.
        self.extra_body = dict(extra_body or {})

    def chat(self, prompt: str, image_bytes: bytes | list[bytes], timeout: int = 120) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in _as_image_list(image_bytes):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(item)},
                }
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
        }
        payload.update(self.extra_body)
        response = self.http_client.post(
            self.api_base,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("model response did not include message content") from exc
        if not isinstance(content, str):
            raise ValueError("model response did not include message content")
        return content


def _data_url(image_bytes: bytes) -> str:
    ext = detect_image_extension(image_bytes)
    mime = MIME_BY_EXT.get(ext, "image/jpeg")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _as_image_list(image_bytes: bytes | list[bytes]) -> list[bytes]:
    if isinstance(image_bytes, list):
        return image_bytes
    return [image_bytes]
