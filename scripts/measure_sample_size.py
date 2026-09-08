#!/usr/bin/env python3
"""量一下样本有多大，以及有多少条根本塞不进推理窗口。

为什么需要这把尺子：整段翻译失败会退化成逐句翻，而**逐句时每一句都要把整套
图片重发一遍**——39 轮 1 图的样本，逐句要烧掉整段的 14.6 倍 token。所以
「有多少条会走回退」直接决定了整轮任务的墙上时间，而这个数以前没人量过。

翻译任务的输出长度约等于输入，所以判据不是「提示词能不能塞进窗口」，而是
「提示词 + 一份等长的译文能不能一起塞进去」。默认按窗口的一半算输入上限。

    python scripts/measure_sample_size.py <数据集目录> --max-model-len 32768

只读 parquet，不连库、不发请求、不落盘。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.parquet_reader import stream_parquet_rows  # noqa: E402
from finevision_to_sharegpt.sample_parser import parse_row  # noqa: E402

# Qwen-VL 把图切成 28x28 的 patch 再 2x2 合并。一张 1024x1024 约 1300 token，
# 这里取个中间值——量级对了就够做决定，差 20% 不影响结论。
TOKENS_PER_IMAGE = 1300
# 英文约 4 字符 1 token；中文译文更密，但输入是英文，按英文算。
CHARS_PER_TOKEN = 4


def sample_tokens(sample) -> tuple[int, int, int]:
    text_chars = sum(len(turn.text) for turn in sample.turns)
    text_tokens = text_chars // CHARS_PER_TOKEN
    image_tokens = len(sample.image_bytes_list) * TOKENS_PER_IMAGE
    return text_tokens, image_tokens, len(sample.turns)


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="数据集目录（下面是 *.parquet）")
    parser.add_argument("--max-model-len", type=int, default=32768,
                        help="实例的 --max-model-len，默认 32768")
    parser.add_argument("--rows", type=int, default=2000, help="每个数据集抽多少行，默认 2000")
    args = parser.parse_args(argv)

    # 输入要给等长的译文留出地方，再扣掉提示词模板本身。
    budget = args.max_model_len // 2 - 500

    root = Path(args.root)
    files = sorted(root.rglob("*.parquet"))
    if not files:
        print(f"{root} 下没有 parquet", file=sys.stderr)
        return 1

    totals: list[int] = []
    turn_counts: list[int] = []
    over = 0
    fallback_cost = 0
    whole_cost = 0
    seen = 0

    for path in files:
        if seen >= args.rows:
            break
        for row in stream_parquet_rows(path):
            if seen >= args.rows:
                break
            result = parse_row(row, f"{path.name}:{seen}")
            if not result.accepted or result.sample is None:
                continue
            seen += 1
            text_tokens, image_tokens, turns = sample_tokens(result.sample)
            total = text_tokens + image_tokens
            totals.append(total)
            turn_counts.append(turns)
            if total > budget:
                over += 1
            whole_cost += total
            fallback_cost += turns * (image_tokens + text_tokens // max(1, turns))

    if not totals:
        print("一条都没解析出来", file=sys.stderr)
        return 1

    print(f"数据集   {root}")
    print(f"样本     {seen} 条（{len(files)} 个 parquet 中抽样）")
    print(f"窗口     max_model_len {args.max_model_len} → 输入预算 {budget} tok"
          f"（一半留给译文，再扣 500 提示词）")
    print()
    print("轮数     " + "  ".join(f"p{int(q*100)}={percentile(turn_counts, q)}"
                                  for q in (0.5, 0.9, 0.99)) + f"  max={max(turn_counts)}")
    print("token    " + "  ".join(f"p{int(q*100)}={percentile(totals, q)}"
                                  for q in (0.5, 0.9, 0.99)) + f"  max={max(totals)}")
    print()
    share = over / seen * 100
    print(f"塞不下   {over} / {seen} 条（{share:.1f}%）会 400 或者被截断 → 走逐句回退")
    if whole_cost:
        print(f"代价     全部整段 {whole_cost/1e6:.1f}M tok，"
              f"全部逐句 {fallback_cost/1e6:.1f}M tok（{fallback_cost/whole_cost:.1f}×）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
