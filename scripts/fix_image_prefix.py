#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把账本里图片路径的第一段改名（如 images/ -> fv_images/）。

``image_paths`` 存的是相对路径，第一段是产出时 ``images_root`` 的目录名，写死进库。
图片目录改名或搬位置之后，库里的前缀就和盘上对不上，而且不报错——训练时图片
静默读不到。改了 ``images_root`` 之后要跑一次这个，把历史行对齐。

默认只统计不写。确认无误再加 ``--apply``。

    python3 scripts/fix_image_prefix.py configs/translate_5m.json --to fv_images
    python3 scripts/fix_image_prefix.py configs/translate_5m.json --to fv_images --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

for _c in (Path(__file__).resolve().parent.parent / "src", Path.cwd() / "src"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))
        break

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="改写账本里图片路径的第一段")
    ap.add_argument("config", help="任务配置，取其中的 mysql 段")
    ap.add_argument("--to", required=True, help="新的第一段，如 fv_images")
    ap.add_argument("--from", dest="old", default=None, help="旧的第一段；不给则自动探测")
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只统计")
    args = ap.parse_args()

    new = args.to.strip("/")
    if not new or "/" in new or '"' in new:
        sys.exit("[FATAL] --to 只能是一个目录名，不带斜杠")

    config = load_zip_task_config(args.config)
    if config.mysql is None:
        sys.exit("[FATAL] 配置里没有 mysql 段")
    # 只读统计和一条 UPDATE，不需要建表。
    ledger = MySQLLedger(config.mysql, ensure_schema=False)
    try:
        def survey(cursor: Any) -> list[tuple[Any, ...]]:
            # 取数组第一个元素再切第一段。按文本切会把 '["' 一起切进来。
            cursor.execute(
                "SELECT SUBSTRING_INDEX("
                "         JSON_UNQUOTE(JSON_EXTRACT(image_paths, '$[0]')), '/', 1"
                "       ) AS prefix, COUNT(*) "
                "  FROM sample_source WHERE image_count > 0 "
                " GROUP BY prefix ORDER BY 2 DESC"
            )
            return list(cursor.fetchall())

        rows = ledger.pool.run(survey)
        if not rows:
            print("库里没有带图的行")
            return 0

        print("当前库里的路径前缀分布：")
        for prefix, count in rows:
            flag = "  <- 目标，已经是对的" if prefix == new else ""
            print(f"    {str(prefix):<20}{int(count):>12}{flag}")

        old = args.old
        if old is None:
            others = [(p, c) for p, c in rows if p and p != new]
            if not others:
                print(f"\n全部已经是 {new!r}，无事可做。")
                return 0
            if len(others) > 1:
                sys.exit(
                    f"\n[FATAL] 有多个旧前缀 {[p for p, _ in others]}，"
                    "自动探测不敢猜。用 --from 指定要改哪一个。"
                )
            old = others[0][0]
        old = str(old).strip("/")
        affected = sum(int(c) for p, c in rows if p == old)
        print(f"\n要把 {old!r} 改成 {new!r}，涉及 {affected} 行。")

        if not args.apply:
            print("这是预览，一行都没改。确认无误后加 --apply。")
            return 0

        def rewrite(cursor: Any) -> int:
            # 匹配带前导引号的 '"<old>/'，所以改完再跑一次不会二次命中，是幂等的。
            cursor.execute(
                "UPDATE sample_source "
                "   SET image_paths = CAST(REPLACE(CAST(image_paths AS CHAR), %s, %s) AS JSON) "
                " WHERE image_count > 0 AND CAST(image_paths AS CHAR) LIKE %s",
                (f'"{old}/', f'"{new}/', f'%"{old}/%'),
            )
            return int(cursor.rowcount or 0)

        print("改写中……几百万行会跑一会儿，别中断。")
        changed = ledger.pool.run(rewrite)
        print(f"已改 {changed} 行。")
        print("复查：")
        for prefix, count in ledger.pool.run(survey):
            print(f"    {str(prefix):<20}{int(count):>12}")
        print("\n下一步：跑 scripts/check_image_paths.py 确认图片真的能读到。")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
