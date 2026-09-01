#!/usr/bin/env python3
"""Survey what is actually in the ledger, read-only.

``db-status`` answers "how far along is this run". This answers the question
you have before a run: what did earlier passes already put in the database,
is the English source text in there, which samples already carry a
translation, and how much space it all takes. Nothing is created or written —
the tables are only read, so it is safe to point at a live load.

    python scripts/db_inventory.py --config configs/translate_5m.json
    python scripts/db_inventory.py --config configs/translate_5m.json --all
    python scripts/db_inventory.py --config configs/translate_5m.json --dataset chartqa
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.db.pool import ConnectionPool, MySQLUnavailable  # noqa: E402

MB = 1024**2

# One pass over sample_source answers every per-dataset question at once.
# Separate COUNT queries would each re-scan millions of rows.
SOURCE_QUERY = """
SELECT dataset, status, lang_assigned, batch_id,
       COUNT(*)                       AS rows_total,
       SUM(conversations IS NOT NULL) AS with_text,
       SUM(image_count)               AS images
FROM sample_source
{where}
GROUP BY dataset, status, lang_assigned, batch_id
"""

TRANSLATION_QUERY = """
SELECT model_name, prompt_version, COUNT(*) AS rows_total,
       COUNT(DISTINCT source_id) AS samples
FROM sample_translation
GROUP BY model_name, prompt_version
"""

TRANSLATED_BY_DATASET = """
SELECT s.dataset, COUNT(DISTINCT t.source_id)
FROM sample_translation t JOIN sample_source s ON s.id = t.source_id
GROUP BY s.dataset
"""

VERSION_QUERY = """
SELECT dataset, COUNT(*), MAX(images_root), MAX(first_seen_at)
FROM dataset_version GROUP BY dataset
"""

STORAGE_QUERY = """
SELECT table_name, table_rows, data_length, index_length
FROM information_schema.tables
WHERE table_schema = %s
ORDER BY data_length + index_length DESC
"""

Query = Callable[..., list[tuple[Any, ...]]]


def summarize(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """Fold the grouped source rows into totals and a per-dataset view."""

    by_status: dict[str, int] = {}
    by_lang: dict[str, int] = {}
    by_batch: dict[str, int] = {}
    datasets: dict[str, dict[str, Any]] = {}
    total = 0
    with_text = 0
    images = 0
    for dataset, status, lang, batch, count, text_count, image_count in rows:
        count = int(count or 0)
        text_count = int(text_count or 0)
        image_count = int(image_count or 0)
        total += count
        with_text += text_count
        images += image_count
        by_status[status] = by_status.get(status, 0) + count
        by_lang[lang or "(未分配)"] = by_lang.get(lang or "(未分配)", 0) + count
        by_batch[batch or "(无批次)"] = by_batch.get(batch or "(无批次)", 0) + count
        entry = datasets.setdefault(
            dataset, {"total": 0, "with_text": 0, "images": 0, "status": {}, "lang": {}}
        )
        entry["total"] += count
        entry["with_text"] += text_count
        entry["images"] += image_count
        entry["status"][status] = entry["status"].get(status, 0) + count
        entry["lang"][lang or "(未分配)"] = entry["lang"].get(lang or "(未分配)", 0) + count
    return {
        "total": total,
        "with_text": with_text,
        "images": images,
        "by_status": by_status,
        "by_lang": by_lang,
        "by_batch": by_batch,
        "datasets": datasets,
    }


def survey(query: Query, dataset: str | None = None) -> dict[str, Any]:
    where, params = ("WHERE dataset = %s", (dataset,)) if dataset else ("", ())
    report: dict[str, Any] = {"filter": dataset}
    report["sources"] = summarize(query(SOURCE_QUERY.format(where=where), params))
    report["translations"] = query(TRANSLATION_QUERY)
    report["translated_by_dataset"] = dict(query(TRANSLATED_BY_DATASET))
    report["versions"] = query(VERSION_QUERY)
    return report


def print_report(report: dict[str, Any], storage: list[tuple[Any, ...]], top: int | None) -> None:
    sources = report["sources"]
    total = sources["total"]
    if not total:
        print("sample_source 里一行都没有——这个库还没灌过，或者连到了别的 database")
        return

    print(f"样本总数 {total:,}，覆盖 {len(sources['datasets'])} 个数据集，图片引用 {sources['images']:,} 次")
    print(f"存了英文原文的 {sources['with_text']:,} 行 ({sources['with_text'] / total:.1%})")
    if sources["with_text"] == 0:
        print("  ⚠️ 一行都没存原文：灌库时 store_conversations 是关的，英文样本导不出来")
    elif sources["with_text"] < total:
        print("  ⚠️ 只有一部分存了原文，说明前后两次灌库的 store_conversations 设置不一致")

    print("\n按状态:")
    for status, count in sorted(sources["by_status"].items(), key=lambda item: -item[1]):
        print(f"  {status:<10}{count:>12,}  {count / total:6.1%}")
    print("按语言:")
    for lang, count in sorted(sources["by_lang"].items(), key=lambda item: -item[1]):
        print(f"  {lang:<10}{count:>12,}  {count / total:6.1%}")
    if len(sources["by_batch"]) > 1 or "(无批次)" not in sources["by_batch"]:
        print("按批次:")
        for batch, count in sorted(sources["by_batch"].items(), key=lambda item: -item[1])[:10]:
            print(f"  {batch:<24}{count:>12,}")

    translations = report["translations"]
    translated = report["translated_by_dataset"]
    print(f"\n译文 {sum(int(row[2]) for row in translations):,} 条，覆盖 {sum(translated.values()):,} 个样本")
    for model, version, rows_total, samples in translations:
        print(f"  {model:<24}{version:<18}{int(rows_total):>12,} 条 / {int(samples):,} 个样本")
    if not translations:
        print("  （还没有任何译文，库里现在只有源样本）")

    datasets = sources["datasets"]
    order = sorted(datasets, key=lambda name: -datasets[name]["total"])
    shown = order if top is None else order[:top]
    print(f"\n{'数据集':<34}{'总数':>10}{'done':>10}{'pending':>10}{'中文':>9}{'英文':>9}{'有译文':>9}")
    for name in shown:
        entry = datasets[name]
        print(
            f"  {name:<32}{entry['total']:>10,}{entry['status'].get('done', 0):>10,}"
            f"{entry['status'].get('pending', 0):>10,}{entry['lang'].get('zh', 0):>9,}"
            f"{entry['lang'].get('en', 0):>9,}{translated.get(name, 0):>9,}"
        )
    if top is not None and len(order) > top:
        print(f"  ... 还有 {len(order) - top} 个，加 --all 全看")

    if storage:
        print(f"\n{'表':<24}{'估算行数':>14}{'数据':>10}{'索引':>10}")
        for table, rows_total, data_length, index_length in storage:
            print(
                f"  {table:<22}{int(rows_total or 0):>14,}"
                f"{int(data_length or 0) / MB:>9.0f}M{int(index_length or 0) / MB:>9.0f}M"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="survey what is in the ledger (read-only)")
    parser.add_argument("--config", required=True, help="task config carrying the mysql section")
    parser.add_argument("--dataset", help="only this dataset")
    parser.add_argument("--top", type=int, default=30, help="how many datasets to list (default 30)")
    parser.add_argument("--all", action="store_true", help="list every dataset")
    args = parser.parse_args(argv)

    config = load_zip_task_config(args.config)
    if config.mysql is None:
        print(f"{args.config} 里没有 mysql 段，没有库可查", file=sys.stderr)
        return 1

    pool = ConnectionPool(config.mysql)

    def query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        def run(cursor: Any) -> list[tuple[Any, ...]]:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

        return pool.run(run)

    try:
        report = survey(query, args.dataset)
        storage = query(STORAGE_QUERY, (config.mysql.database,))
    except MySQLUnavailable as exc:
        # The pool wraps every failure, so a missing table and an unreachable
        # server arrive as the same type. Name both rather than guessing.
        print(f"查询失败: {exc}", file=sys.stderr)
        print("要么连不上库，要么表还没建（先跑 db-init）", file=sys.stderr)
        return 1
    finally:
        pool.close()

    print_report(report, storage, None if args.all else args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
