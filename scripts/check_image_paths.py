#!/usr/bin/env python3
"""抽样验证库里的图片路径能不能在盘上找到。

库里存的是相对路径（``images/<数据集>/<sha256>.<ext>``），能不能读到取决于
拼在哪个根下面。这个脚本把候选根都试一遍，报命中率，省得靠猜。

    python3 check_images.py configs/translate_5m.json <候选根1> [候选根2 ...]

不给候选根就用配置推出来的那个。
"""

import random
import sys
from pathlib import Path

for _c in (Path(__file__).resolve().parent / "src", Path.cwd() / "src"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))
        break

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger, _load_json  # noqa: E402

SAMPLE = 200


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("用法: python3 check_images.py <任务配置> [候选根 ...]")
    config = load_zip_task_config(sys.argv[1])
    roots = [Path(p) for p in sys.argv[2:]] or [config.images_root.parent]

    ledger = MySQLLedger(config.mysql, ensure_schema=False)
    try:
        def query(cursor):
            cursor.execute(
                "SELECT dataset, image_paths FROM sample_source "
                "WHERE status='done' AND image_count>0 ORDER BY id LIMIT %s",
                (SAMPLE * 5,),
            )
            return list(cursor.fetchall())

        rows = ledger.pool.run(query)
    finally:
        ledger.close()

    if not rows:
        print("库里没有带图的 done 行")
        return 1

    paths = []
    for dataset, blob in rows:
        for p in _load_json(blob) or []:
            paths.append((dataset, str(p)))
    random.seed(0)
    sample = random.sample(paths, min(SAMPLE, len(paths)))

    print(f"抽了 {len(sample)} 条路径，库里长这样：")
    for _, p in sample[:3]:
        print(f"    {p}")
    print(f"\n配置推出来的 images_root = {config.images_root}")
    print(f"（前缀就是它的目录名: {config.images_root.name!r}）\n")

    best = None
    for root in roots:
        hit = sum(1 for _, p in sample if (root / p).is_file())
        rate = hit / len(sample) * 100
        mark = "✅" if rate > 99 else ("⚠️" if rate > 0 else "❌")
        print(f"  {mark} {root}  命中 {hit}/{len(sample)} ({rate:.0f}%)")
        if best is None or hit > best[1]:
            best = (root, hit)
        if rate == 0 and root.is_dir():
            # 一条都没命中，最常见的原因是图片目录被改了名：库里写死的前缀是
            # images_root 的目录名，盘上却换成了别的。拿根下面实际存在的子目录
            # 逐个当替代前缀试一遍，直接把答案指出来，省得人肉对。
            stored_prefix = Path(sample[0][1]).parts[0]
            for child in sorted(x for x in root.iterdir() if x.is_dir()):
                hit2 = sum(
                    1
                    for _, q in sample
                    if (root / child.name / Path(*Path(q).parts[1:])).is_file()
                )
                if hit2:
                    print(
                        f"       但把前缀 {stored_prefix!r} 换成 {child.name!r} 后命中 "
                        f"{hit2}/{len(sample)}。"
                    )
                    print(
                        f"       → 图片目录被改过名。要么把 {child} 改回叫 "
                        f"{stored_prefix!r}，要么做个软链："
                    )
                    print(f"         ln -s {child} {root / stored_prefix}")
                    break

    print()
    if best and best[1] == len(sample):
        print(f"结论：训练时把图片根指到  {best[0]}  即可，路径不用改。")
    elif best and best[1]:
        print(f"结论：{best[0]} 只命中一部分，同一批数据里混了两种前缀，得先统一。")
    else:
        print("结论：没有一个候选根能命中。把图片实际所在目录当参数再跑一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
