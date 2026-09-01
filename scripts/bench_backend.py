#!/usr/bin/env python3
"""Measure one backend's latency and throughput, with and without an image.

``check_backends.py`` answers "is it up". This answers "how fast", which is
the number a multi-day run is planned against. Two things are separated
because they fail differently: single-request latency exposes a sick or
contended instance, while throughput at concurrency exposes how much of the
GPU a run can actually keep busy. An image is sent in half the rounds because
the translation pipeline always sends one, and its prefill usually costs far
more than the handful of tokens the answer takes.

    python scripts/bench_backend.py --port 8002
    python scripts/bench_backend.py --port 8002 --concurrency 64 --requests 128
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.config_loader import load_backend_config  # noqa: E402

PROMPT = "把这句话翻成中文，只输出译文：The cat sat on the mat."

# A 1x1 red JPEG. Big enough to exercise the vision tower's prefill path,
# small enough that upload time is never what is being measured.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def one_request(url: str, key: str, model: str, with_image: bool, timeout: int) -> tuple[float, int]:
    content: list[dict] = [{"type": "text", "text": PROMPT}]
    if with_image:
        encoded = base64.b64encode(TINY_JPEG).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 128,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    # An explicit empty ProxyHandler: a proxy in the environment would otherwise
    # swallow requests to a loopback address.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start = time.time()
    data = json.load(opener.open(request, timeout=timeout))
    return time.time() - start, int(data["usage"]["completion_tokens"])


def run_round(url, key, model, with_image, requests, concurrency, timeout) -> None:
    label = "带图" if with_image else "纯文本"
    started = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(one_request, url, key, model, with_image, timeout) for _ in range(requests)
        ]
        results, errors = [], []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - a failed request is a data point
                errors.append(str(exc))
    wall = time.time() - started
    if not results:
        print(f"  {label:<8} 全部失败: {errors[0][:80] if errors else '?'}")
        return
    latencies = sorted(item[0] for item in results)
    tokens = sum(item[1] for item in results)
    print(
        f"  {label:<8} {len(results)}/{requests} 成功  "
        f"耗时 {wall:5.1f}s  吞吐 {len(results) / wall:6.2f} 条/秒  "
        f"延迟 中位 {statistics.median(latencies):5.1f}s / p95 {latencies[int(len(latencies) * 0.95) - 1]:5.1f}s  "
        f"输出 {tokens / len(results):.0f} token/条"
    )
    if errors:
        print(f"           {len(errors)} 个失败，例: {errors[0][:80]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure a backend's latency and throughput")
    parser.add_argument("--config", default="configs/backend_config.json")
    parser.add_argument("--port", type=int, help="only the backend on this port")
    parser.add_argument("--name", help="only the backend with this name")
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    config = load_backend_config(args.config)
    backends = config.backends
    if args.port:
        backends = [item for item in backends if f":{args.port}/" in item.api_base]
    if args.name:
        backends = [item for item in backends if item.name == args.name]
    if not backends:
        print("没有匹配的后端", file=sys.stderr)
        return 1

    for backend in backends:
        print(f"\n=== {backend.name}  {backend.api_base}  model={backend.model}")
        print("  单条延迟（并发 1，看实例本身健不健康）")
        run_round(backend.api_base, backend.api_key, backend.model, False, 1, 1, args.timeout)
        run_round(backend.api_base, backend.api_key, backend.model, True, 1, 1, args.timeout)
        print(f"  并发 {args.concurrency} × {args.requests} 条（看真实吞吐）")
        run_round(
            backend.api_base, backend.api_key, backend.model, False,
            args.requests, args.concurrency, args.timeout,
        )
        run_round(
            backend.api_base, backend.api_key, backend.model, True,
            args.requests, args.concurrency, args.timeout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
