#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从纯文本语料按预算抽 CPT 训练集，跨轮不重复。

多模态那条路收不了纯文本（`parse_row` 没有图片字节就判 `missing_image`），而
Ultra-FineWeb 这种语料也不该按那套存：11.8 亿行、1.7 TB，正文进库不现实，
连「一行一条」的账本都有 110 GB。

所以这里**不建行级账本**，只用 `dataset_cursor` 记到每个分片读到第几行——
一个语料几百上千行水位线，不是几亿。正文留在 parquet 里，导出时直接写进 jsonl。
下次跑从水位线接着往后读，天然不重复。

分片是**按种子随机挑**的，不是从头顺着拿：语料分片可能带顺序（抓取时间、来源），
永远从第一个分片开始会抽出有偏的子集。挑中的分片从各自的水位线往后读。

    python3 scripts/export_cpt_mix.py --config configs/cpt_example.json --dry-run
    python3 scripts/export_cpt_mix.py --config configs/cpt_example.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Iterator

for _c in (Path(__file__).resolve().parent.parent / "src", Path.cwd() / "src"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))
        break

from finevision_to_sharegpt.db import load_mysql_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger  # noqa: E402


def load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            sys.exit("[FATAL] 没装 pyyaml。把配置改成 .json 即可，字段完全一样。")
        return yaml.safe_load(text)
    return json.loads(text)


def iter_text(path: Path, column: str, start_row: int, batch: int = 2000) -> Iterator[tuple[int, str]]:
    """从 ``start_row`` 开始产出 (行号, 正文)，只读正文那一列。

    只取一列是关键：Ultra-FineWeb 的行有 uid/content/style，而 content 就是
    几乎全部字节。按列读能让 parquet 跳过其余列，也能整组跳过已读的部分。
    """

    import pyarrow.parquet as pq

    handle = pq.ParquetFile(path)
    offset = 0
    for group in range(handle.num_row_groups):
        size = handle.metadata.row_group(group).num_rows
        if offset + size <= start_row:      # 整组都在水位线之下，连读都不读
            offset += size
            continue
        table = handle.read_row_group(group, columns=[column])
        values = table[column].to_pylist()
        for index, value in enumerate(values):
            row = offset + index
            if row < start_row or value is None:
                continue
            yield row, str(value)
        offset += size


def main() -> int:
    ap = argparse.ArgumentParser(description="按预算从纯文本语料抽 CPT 训练集")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=None, help="覆盖配置里的 output")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="只算要读哪些分片，不写文件")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    cfg_hash = hashlib.sha256(cfg_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:12]

    corpora = cfg.get("corpora") or []
    if not corpora:
        sys.exit("[FATAL] 配置里没有 corpora")
    if not cfg.get("mysql"):
        sys.exit("[FATAL] 配置里没有 mysql 段——水位线要存在库里，否则续跑会重复")
    seed = args.seed if args.seed is not None else cfg.get("seed", 42)
    out_dir = Path(args.out or cfg.get("output") or "./out/cpt")

    mysql = load_mysql_config(cfg["mysql"])
    if "${" in (mysql.password or ""):
        sys.exit("[FATAL] 密码没展开，先 export FV_MYSQL_PASSWORD=...")
    # 要写 dataset_cursor，所以不能用 ensure_schema=False。
    ledger = MySQLLedger(mysql, batch_id=cfg.get("batch_id"))
    t0 = time.time()
    stats: list[dict[str, Any]] = []

    try:
        for spec in corpora:
            name = spec["name"]
            root = Path(spec["path"])
            column = spec.get("text_column", "content")
            if not root.is_dir():
                sys.exit(f"[FATAL] {name}: 不是目录 {root}")

            docs_budget = spec.get("target_docs")
            token_budget = spec.get("target_tokens")
            cpt = float(spec.get("chars_per_token", 4.0))
            if not docs_budget and not token_budget:
                sys.exit(f"[FATAL] {name}: 要给 target_docs 或 target_tokens")

            print(f"\n=== {name} ===")
            # 声明成 pt：CPT 语料和多模态数据集共用 dataset_version，
            # 不标格式就只能靠名字认。
            version = ledger.open_dataset(name, root, out_dir, data_format="pt")
            shards = sorted(p for p in root.rglob("*.parquet") if p.is_file())
            print(f"  {len(shards)} 个分片，水位线存在 dataset_cursor")

            # 按种子定分片顺序。语料分片可能带顺序，从头顺着拿会抽出有偏的子集。
            order = list(shards)
            random.Random(seed).shuffle(order)

            if args.dry_run:
                # 报进度而不是逐个分片查水位线：1168 个分片就是 1168 次查询，
                # 而真实条数不读文件也算不出来，查了也没意义。
                consumed = ledger.cursor_progress(version)
                avg = spec.get("avg_chars") or 0
                rows_per_shard = spec.get("rows_per_shard") or 0
                print(f"  已消费 {consumed['shards']}/{len(shards)} 个分片，"
                      f"累计约 {consumed['rows']:,} 行")
                if docs_budget:
                    need = docs_budget
                    print(f"  本轮目标 {need:,} 条", end="")
                elif avg:
                    need = int(token_budget * cpt / avg)
                    print(f"  本轮目标 {token_budget:,} tokens ≈ {need:,} 条", end="")
                else:
                    need = 0
                    print(f"  本轮目标 {token_budget:,} tokens"
                          f"（给 avg_chars 才能换算成条数）", end="")
                if need and rows_per_shard:
                    print(f"，约需 {need / rows_per_shard:.1f} 个分片")
                else:
                    print()
                stats.append({"name": name, "path": str(root),
                              "shards_total": len(shards), "dry_run": True,
                              "consumed": consumed})
                continue

            written = 0
            chars = 0
            touched: list[dict[str, Any]] = []
            handle = None
            out_path = out_dir / f"{name}.jsonl"
            if True:
                out_dir.mkdir(parents=True, exist_ok=True)
                handle = out_path.open("w", encoding="utf-8")

            try:
                for shard in order:
                    if docs_budget and written >= docs_budget:
                        break
                    if token_budget and chars / cpt >= token_budget:
                        break
                    rel = shard.relative_to(root).as_posix()
                    plan = ledger.scan_plan(version, rel, for_ingest=True)
                    took = 0
                    last = plan.start_row - 1
                    for row, text in iter_text(shard, column, plan.start_row):
                        handle.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
                        written += 1
                        took += 1
                        chars += len(text)
                        last = row
                        if docs_budget and written >= docs_budget:
                            break
                        if token_budget and chars / cpt >= token_budget:
                            break
                    if took:
                        ledger.note_scanned(version, rel, last)
                        touched.append({"shard": rel, "from": plan.start_row, "rows": took})
                        print(f"    {rel:<64} 第 {plan.start_row:>9,} 行起，取 {took:>8,}")
            finally:
                if handle is not None:
                    handle.close()
                # 水位线最后统一落库：中途崩了宁可下次重读，也不能记了没产出的行。
                ledger.flush()

            tokens = chars / cpt
            stats.append({
                "name": name, "path": str(root), "shards_total": len(shards),
                "shards_touched": len(touched), "docs": written,
                "chars": chars, "est_tokens": int(tokens),
                "chars_per_token": cpt, "detail": touched,
            })
            if True:
                print(f"  产出 {written:,} 条，{chars:,} 字符 ≈ {tokens/1e8:.2f} 亿 tokens"
                      f"，动了 {len(touched)}/{len(shards)} 个分片")
                print(f"  -> {out_path}")
    finally:
        ledger.close()

    if args.dry_run:
        print(f"\n[dry-run] 没有写文件。耗时 {time.time() - t0:.0f}s")
        return 0

    info = {
        s["name"]: {"file_name": f"{s['name']}.jsonl", "columns": {"prompt": "text"}}
        for s in stats
    }
    (out_dir / "dataset_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config_file": str(cfg_path), "config_sha256_12": cfg_hash,
        "seed": seed, "output": str(out_dir), "corpora": stats,
        "total_docs": sum(s["docs"] for s in stats),
        "total_est_tokens": sum(s["est_tokens"] for s in stats),
        "note": "水位线记在 dataset_cursor，下次跑从这之后继续，不会重复。",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n合计 {manifest['total_docs']:,} 条 ≈ "
          f"{manifest['total_est_tokens']/1e8:.2f} 亿 tokens")
    print(f"dataset_info.json / manifest.json -> {out_dir}")
    print(f"总耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
