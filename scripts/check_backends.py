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

import argparse
import base64
import json
import time
from pathlib import Path

import httpx

# A 1x1 JPEG: the smallest thing that proves the endpoint accepts image parts.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def probe(backend: dict, timeout: int, with_image: bool) -> tuple[bool, str, float]:
    content: list[dict] = [{"type": "text", "text": "Reply with the single word: ok"}]
    if with_image:
        encoded = base64.b64encode(TINY_JPEG).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    payload = {
        "model": backend["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 16,
    }
    started = time.monotonic()
    try:
        response = httpx.post(
            backend["api_base"],
            headers={
                "Authorization": f"Bearer {backend.get('api_key', '')}",
                "Content-Type": "application/json",
            },
            json=payload,
            # Separate budgets: a refused connection is a wrong address, a
            # connection that opens then stalls is a slow or cold model.
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
        )
    except httpx.ConnectError as exc:
        return False, f"cannot reach the host: {exc}", time.monotonic() - started
    except httpx.ConnectTimeout:
        return False, "connection timed out: host is not answering", time.monotonic() - started
    except httpx.ReadTimeout:
        elapsed = time.monotonic() - started
        return False, f"connected, but no reply within {elapsed:.0f}s", elapsed
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", time.monotonic() - started
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        body = response.text.strip().replace("\n", " ")
        return False, f"HTTP {response.status_code}: {body[:300]}", elapsed
    try:
        reply = response.json()["choices"][0]["message"]["content"]
    except Exception:
        return False, f"unexpected response shape: {response.text[:300]}", elapsed
    return True, str(reply).strip().replace("\n", " ")[:100], elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="probe the backends in a backend config")
    parser.add_argument("config", nargs="?", default="configs/backend_config.json")
    parser.add_argument("--timeout", type=int, help="read timeout in seconds (default: request_timeout)")
    parser.add_argument("--only", help="probe just this backend name")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    timeout = args.timeout or int(config.get("request_timeout", 120))
    backends = config.get("backends") or []
    if args.only:
        backends = [item for item in backends if item.get("name") == args.only]
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

        ok_text, detail_text, secs_text = probe(backend, timeout, with_image=False)
        print(f"    text  : {'OK' if ok_text else 'FAIL'}  [{secs_text:5.1f}s]  {detail_text}")
        ok_image, detail_image, secs_image = probe(backend, timeout, with_image=True)
        print(f"    image : {'OK' if ok_image else 'FAIL'}  [{secs_image:5.1f}s]  {detail_image}")

        if ok_text and not ok_image:
            print("    [fatal] this model rejects images. Translation sends every sample's")
            print("            images, so it would fail on all of them. Use a vision model.")
        if not ok_text and "no reply within" in detail_text:
            print("    [hint] the route exists (a GET returns 405 Method Not Allowed) but")
            print("           inference is slow or the deployment is cold. Retry with")
            print(f"           --timeout {max(timeout * 3, 300)} --only {name}, and if it only")
            print("           answers slowly, give it a low concurrency or drop it.")
        if ok_text and secs_text > 30:
            print(f"    [warn] {secs_text:.0f}s for a 16-token reply; raise request_timeout")
            print("           and keep concurrency low for this backend")
        if "<think>" in detail_text or "<think>" in detail_image:
            print("    [note] this model emits reasoning blocks; they are stripped before")
            print("           parsing. Set extra_body.chat_template_kwargs.enable_thinking")
            print("           to false to stop paying for tokens that get discarded.")
        if not (ok_text and ok_image):
            failures += 1

    print()
    print(f"{len(backends) - failures}/{len(backends)} backends usable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
