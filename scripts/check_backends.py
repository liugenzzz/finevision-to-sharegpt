#!/usr/bin/env python3
"""Probe every backend in a backend config before starting a long run.

Checks the three things that silently waste hours otherwise:

1. the endpoint is reachable and ``api_base`` points at the full
   ``/v1/chat/completions`` path rather than just ``/v1``;
2. the API key is accepted;
3. the model accepts an image. The translation pipeline always sends the
   sample's images as base64 ``image_url`` parts, so a text-only model fails
   on every single record.

Usage:
    python scripts/check_backends.py configs/backend_config.json
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx

# A 1x1 JPEG: the smallest thing that proves the endpoint accepts image parts.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def probe(backend: dict, timeout: int, with_image: bool) -> tuple[bool, str]:
    content: list[dict] = [{"type": "text", "text": "Reply with the single word: ok"}]
    if with_image:
        encoded = base64.b64encode(TINY_JPEG).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    payload = {
        "model": backend["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 16,
    }
    try:
        response = httpx.post(
            backend["api_base"],
            headers={
                "Authorization": f"Bearer {backend.get('api_key', '')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        body = response.text.strip().replace("\n", " ")
        return False, f"HTTP {response.status_code}: {body[:300]}"
    try:
        reply = response.json()["choices"][0]["message"]["content"]
    except Exception:
        return False, f"unexpected response shape: {response.text[:300]}"
    return True, str(reply).strip()[:120]


def main(argv: list[str]) -> int:
    config_path = Path(argv[1] if len(argv) > 1 else "configs/backend_config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    timeout = int(config.get("request_timeout", 120))
    backends = config.get("backends") or []
    if not backends:
        print(f"{config_path} declares no backends")
        return 1

    failures = 0
    for backend in backends:
        name = backend.get("name", "?")
        print(f"=== {name}  model={backend.get('model')}")
        print(f"    {backend.get('api_base')}")
        if not str(backend.get("api_base", "")).rstrip("/").endswith("/chat/completions"):
            print("    [warn] api_base does not end with /chat/completions;")
            print("           the Base URL from a model listing usually needs that appended")
        if "填" in str(backend.get("api_key", "")) or not backend.get("api_key"):
            print("    [skip] api_key is still a placeholder")
            failures += 1
            continue

        ok_text, detail_text = probe(backend, timeout, with_image=False)
        print(f"    text  : {'OK' if ok_text else 'FAIL'}  {detail_text}")
        ok_image, detail_image = probe(backend, timeout, with_image=True)
        print(f"    image : {'OK' if ok_image else 'FAIL'}  {detail_image}")
        if ok_text and not ok_image:
            print("    [fatal] this model rejects images. Translation sends every sample's")
            print("            images, so it would fail on all of them. Use a vision model.")
        if not (ok_text and ok_image):
            failures += 1

    print()
    print(f"{len(backends) - failures}/{len(backends)} backends usable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
