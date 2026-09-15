#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看一眼纯文本语料长什么样：文件格式、列、行数量级、样例。

多模态那条管线用不了纯文本——`parse_row` 没有图片字节就直接判 `missing_image`，
在看文本之前就拒了。CPT 语料要另开一条入库路径，而怎么设计取决于这里看到的
列名、字段语义和规模，所以先探再建。

只读，不碰数据库。

    python3 scripts/probe_text_corpus.py /mnt/.../Ultra-FineWeb-L3
    python3 scripts/probe_text_corpus.py /mnt/.../Ultra-FineWeb-L3 --rows 3 --chars 600
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

GB = 1024**3
MB = 1024**2
# 看着像正文的列名，只用来给个提示，最终以人眼确认为准。
TEXT_HINTS = ("text", "content", "raw_content", "document", "body", "passage")


def human(size: int) -> str:
    return f"{size / GB:.2f} GB" if size >= GB else f"{size / MB:.1f} MB"


def preview(value: Any, chars: int) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} 字节的二进制>"
    text = str(value).replace("\n", "\\n")
    return text[:chars] + (f" …（共 {len(str(value))} 字符）" if len(text) > chars else "")


def probe_parquet(files: list[Path], rows: int, chars: int) -> None:
    import pyarrow.parquet as pq

    first = pq.ParquetFile(files[0])
    schema = first.schema_arrow
    total_rows = 0
    for f in files[:50]:            # 全量取元数据太慢，前 50 个够外推
        try:
            total_rows += pq.ParquetFile(f).metadata.num_rows
        except Exception as exc:  # noqa: BLE001
            print(f"    [警告] {f.name} 读元数据失败: {exc}")
    scale = len(files) / min(len(files), 50)
    print(f"\n  行数：抽样 {min(len(files), 50)} 个分片共 {total_rows:,} 行"
          f"，按此外推全部约 {total_rows * scale:,.0f} 行")

    print(f"\n  列（{len(schema.names)} 个）：")
    for name, field in zip(schema.names, schema):
        hint = "   <- 像正文" if name.lower() in TEXT_HINTS else ""
        print(f"    {name:<28}{str(field.type):<24}{hint}")

    # 篇长统计：估 token 要的是均值和分位数，一行样例看不出来。
    # 只读第一个分片的前几个 row group，够用且不用扫 1700 个文件。
    text_col = next((n for n in schema.names if n.lower() in TEXT_HINTS), None)
    if text_col:
        lengths: list[int] = []
        for group in range(min(4, first.num_row_groups)):
            column = first.read_row_group(group, columns=[text_col])[text_col]
            lengths.extend(len(str(v)) for v in column.to_pylist() if v is not None)
        if lengths:
            lengths.sort()
            n = len(lengths)
            mean = sum(lengths) / n
            print(f"\n  `{text_col}` 篇长（抽样 {n:,} 行）：")
            print(f"    平均 {mean:>9,.0f} 字符")
            for label, q in (("中位数", 0.5), ("P90", 0.9), ("P99", 0.99)):
                print(f"    {label:<7}{lengths[min(n - 1, int(n * q))]:>9,} 字符")
            print(f"    最短 {lengths[0]:,} / 最长 {lengths[-1]:,}")
            print("\n  按均值估 token（中文约 1.2~1.6 字符/token，英文约 4）：")
            for cpt in (1.2, 1.6, 4.0):
                print(f"    {cpt} 字符/token → 每篇 {mean / cpt:>7,.0f} tokens"
                      f"，500 万条 ≈ {5e6 * mean / cpt / 1e8:>7.1f} 亿")

    print(f"\n  前 {rows} 行：")
    table = first.read_row_group(0) if first.num_row_groups else None
    if table is None:
        print("    第一个分片没有 row group")
        return
    data = table.slice(0, rows).to_pylist()
    for i, row in enumerate(data):
        print(f"    --- 第 {i} 行 ---")
        for key, value in row.items():
            print(f"      {key:<26}{preview(value, chars)}")


def probe_jsonl(files: list[Path], rows: int, chars: int) -> None:
    print("\n  取第一个文件的前几行看结构（jsonl 没有元数据，行数只能靠大小估）")
    keys: dict[str, int] = {}
    shown = 0
    with files[0].open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print("    [警告] 第一行不是合法 JSON，可能不是 jsonl")
                return
            for k in obj:
                keys[k] = keys.get(k, 0) + 1
            if shown < rows:
                print(f"    --- 第 {shown} 行 ---")
                for key, value in obj.items():
                    print(f"      {key:<26}{preview(value, chars)}")
                shown += 1
            if shown >= rows and len(keys) > 3:
                break
    print(f"\n  见到的字段：{', '.join(sorted(keys))}")


def main() -> int:
    ap = argparse.ArgumentParser(description="探测纯文本语料的结构")
    ap.add_argument("path", help="语料目录")
    ap.add_argument("--rows", type=int, default=2, help="打印几行样例")
    ap.add_argument("--chars", type=int, default=400, help="每个字段截断到多少字符")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        sys.exit(f"不是目录: {root}")

    by_ext: dict[str, list[Path]] = {}
    for item in root.rglob("*"):
        if item.is_file():
            by_ext.setdefault(item.suffix.lower(), []).append(item)
    if not by_ext:
        sys.exit(f"{root} 下面没有文件")

    print(f"目录 {root}")
    print("\n  文件构成：")
    for ext, files in sorted(by_ext.items(), key=lambda kv: -sum(f.stat().st_size for f in kv[1])):
        size = sum(f.stat().st_size for f in files)
        print(f"    {ext or '(无扩展名)':<14}{len(files):>6} 个   {human(size):>12}")
        for f in sorted(files)[:3]:
            print(f"        {f.relative_to(root)}")
        if len(files) > 3:
            print(f"        …另 {len(files) - 3} 个")

    for ext in (".parquet", ".jsonl", ".json", ".gz"):
        if ext in by_ext:
            print(f"\n{'=' * 60}\n{ext} 详情")
            if ext == ".parquet":
                probe_parquet(sorted(by_ext[ext]), args.rows, args.chars)
            elif ext in (".jsonl", ".json"):
                probe_jsonl(sorted(by_ext[ext]), args.rows, args.chars)
            else:
                print("  .gz 需要先解压才能看，或者告诉我里面是什么格式")
            break
    else:
        print("\n没认出可读的格式。把上面的文件构成贴给我。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
