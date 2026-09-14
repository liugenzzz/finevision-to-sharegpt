#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按抽样计划里的类别定义回填 sample_source.category。

抽样时按类别取候选，`category` 落到列上之后 `(category, status, id)` 就是覆盖
索引，一段连续扫描；否则每组都要 `dataset IN (18~40 个值)` 扫十几二十段再归并
排序，几百万行一组，这是导出耗时的大头。

类别定义只有一份——抽样计划里的 `categories.<名>.match`。**不把它写进数据集
注册表**：同一份分类落在两个地方迟早会分叉，而重跑这个脚本就能按新定义重贴标签。

灌库之后跑一次即可，幂等：

    python3 scripts/fill_category.py configs/translate_5m.json --plan configs/sampling_plan_5m.json
    python3 scripts/fill_category.py configs/translate_5m.json --plan configs/sampling_plan_5m.json --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

for _c in (Path(__file__).resolve().parent.parent / "src", Path.cwd() / "src"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))
        break

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger  # noqa: E402


def load_mapping(plan_path: Path) -> dict[str, str]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    categories = plan.get("categories") or {}
    mapping: dict[str, str] = {}
    clashes: list[str] = []
    for name, spec in categories.items():
        for dataset in spec.get("match") or []:
            if dataset in mapping and mapping[dataset] != name:
                clashes.append(f"{dataset}: {mapping[dataset]} / {name}")
            mapping[dataset] = name
    if clashes:
        sys.exit("[FATAL] 同一个数据集被归进多个类别：\n  " + "\n  ".join(clashes))
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 sample_source.category")
    ap.add_argument("config", help="任务配置，取其中的 mysql 段")
    ap.add_argument("--plan", required=True, help="抽样计划 json，取 categories.*.match")
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只统计")
    args = ap.parse_args()

    mapping = load_mapping(Path(args.plan))
    print(f"类别定义：{len(set(mapping.values()))} 个类别，覆盖 {len(mapping)} 个数据集")

    config = load_zip_task_config(args.config)
    if config.mysql is None:
        sys.exit("[FATAL] 配置里没有 mysql 段")
    ledger = MySQLLedger(config.mysql, ensure_schema=False)
    try:
        def survey(cursor: Any) -> list[tuple[Any, ...]]:
            cursor.execute(
                "SELECT dataset, category, COUNT(*) FROM sample_source GROUP BY dataset, category"
            )
            return list(cursor.fetchall())

        rows = ledger.pool.run(survey)
        if not rows:
            print("库里没有行")
            return 0

        todo: dict[str, tuple[str, int]] = {}
        unknown: dict[str, int] = {}
        already = 0
        for dataset, current, count in rows:
            want = mapping.get(str(dataset))
            if want is None:
                unknown[str(dataset)] = unknown.get(str(dataset), 0) + int(count)
                continue
            if str(current or "") == want:
                already += int(count)
                continue
            prev, prev_n = todo.get(str(dataset), ("", 0))
            todo[str(dataset)] = (want, prev_n + int(count))

        pending = sum(n for _, n in todo.values())
        print(f"已经贴好标签 {already} 行；待贴 {pending} 行，涉及 {len(todo)} 个数据集")
        if unknown:
            total_unknown = sum(unknown.values())
            print(f"\n[注] {len(unknown)} 个数据集不在类别定义里，共 {total_unknown} 行，会保持空：")
            for name, n in sorted(unknown.items(), key=lambda kv: -kv[1])[:8]:
                print(f"       {name:<40}{n:>12}")
            print("     多半是计划里 _excluded 的那些（纯文本等），空着就对了。")

        if not todo:
            print("\n无事可做。")
            return 0
        if not args.apply:
            print("\n这是预览，一行都没改。确认无误后加 --apply。")
            return 0

        print("\n按数据集逐个更新——一条 UPDATE 打全表会长时间持锁，几百万行时很难看。")
        done = 0
        for dataset, (want, count) in sorted(todo.items(), key=lambda kv: -kv[1][1]):
            def update(cursor: Any, dataset: str = dataset, want: str = want) -> int:
                cursor.execute(
                    "UPDATE sample_source SET category = %s "
                    " WHERE dataset = %s AND category <> %s",
                    (want, dataset, want),
                )
                return int(cursor.rowcount or 0)

            changed = ledger.pool.run(update)
            done += changed
            print(f"    {dataset:<40} -> {want:<22}{changed:>10}")
        print(f"\n共更新 {done} 行。")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
