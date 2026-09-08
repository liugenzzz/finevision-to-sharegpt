from __future__ import annotations

import base64
from typing import Any

import httpx

from .image_store import detect_image_extension
from .models import ContextOverflow, TruncatedResponse


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
        try:
            response.raise_for_status()
        except Exception as exc:
            _raise_with_server_reason(response, exc)
        data = response.json()
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("model response did not include message content") from exc
        if not isinstance(content, str):
            raise ValueError("model response did not include message content")
        # 到顶截断是 HTTP 200，content 是半截的：不在这里认出来，下游只会看到
        # "不是合法 JSON"，然后一路回退到逐句翻，而真正的原因是窗口不够。
        if choice.get("finish_reason") == "length":
            raise TruncatedResponse(
                f"response hit the token ceiling (finish_reason=length) "
                f"after {len(content)} chars; the window has no room for the translation"
            )
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


# vLLM 超长时的 400 body 长这样：
# {"object":"error","message":"This model's maximum context length is 32768
#  tokens. However, you requested 41207 tokens ...","type":"BadRequestError"}
_OVERFLOW_MARKERS = ("maximum context length", "longer than the maximum", "max_model_len")


def _raise_with_server_reason(response: Any, original: Exception) -> None:
    """把服务端说的原因带出来，而不是只带一个状态码。

    httpx 的 raise_for_status() 只给 "Client error '400 Bad Request' for url ..."，
    body 里那句 "maximum context length is 32768 tokens" 一个字都不会出现。
    上层于是只能看到一个光秃秃的 400，既不知道是超长还是别的，也就没法分开统计——
    现场就是因此花了很久才定位到「长多轮把窗口打爆」。
    """

    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or ""
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
    except Exception:
        detail = ""
    detail = (str(detail).strip() or str(getattr(response, "text", "")).strip())[:500]
    if any(marker in detail.lower() for marker in _OVERFLOW_MARKERS):
        raise ContextOverflow(detail) from original
    raise ValueError(f"{original} | {detail}" if detail else str(original)) from original
