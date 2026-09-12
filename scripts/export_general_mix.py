#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按配比从通用账本抽训练集（配置文件驱动）。

和领域侧的 ``export_mix.py`` 是同一套语义——weight/count 混用、on_shortfall
四种模式、seed 固定可复现、dry-run 先看比例、旁边写 manifest——只是数据源换成
本项目的账本（``sample_source`` + ``sample_translation``），过滤维度也换成通用
侧真正有的那些：数据集名、语言、有没有图。

通用侧比领域侧多一个必需的东西：``max_share_per_dataset``。FineVision 的类别里
数据集大小差两个数量级，densefusion_1m 一家 105 万，不封顶的话它能吃掉 caption
类配额的一半，抽出来的"通用数据"其实是一个数据集的复读。``balance_by`` 的等分
在这里又太硬——小数据集根本填不满份额。所以两个都支持，按需选。

用法：
    python3 scripts/export_general_mix.py --config configs/general_mix_example.json --dry-run
    python3 scripts/export_general_mix.py --config configs/general_mix_example.json

配置里的 mysql 段沿用任务配置那一套，密码走 ${FV_MYSQL_PASSWORD}。
离线环境没装 pyyaml 就用 .json，字段完全一样。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

for _candidate in (Path(__file__).resolve().parent.parent / "src", Path.cwd() / "src"):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
        break

from finevision_to_sharegpt.db import load_mysql_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger  # noqa: E402

SHORTFALL_MODES = ("scale", "take", "repeat", "error")


def load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            sys.exit("[FATAL] 没装 pyyaml。把配置改成 .json 即可，字段完全一样。")
        return yaml.safe_load(text)
    return json.loads(text)


def build_where(f: dict[str, Any]) -> tuple[str, list[Any]]:
    """filter 字典 -> (where 子句, 参数)。值给 list 表示 IN。

    默认只取 ``done``：``pending``/``rejected`` 的行没有可用的产出，混进训练集
    就是空样本。想要别的状态得显式写。
    """

    conds: list[str] = []
    params: list[Any] = []

    def in_or_eq(col: str, val: Any, negate: bool = False) -> None:
        if isinstance(val, (list, tuple)):
            if not val:
                return
            ph = ",".join(["%s"] * len(val))
            conds.append(f"{col} {'NOT IN' if negate else 'IN'} ({ph})")
            params.extend([str(v) for v in val])
        else:
            conds.append(f"{col} {'<>' if negate else '='} %s")
            params.append(str(val))

    conds.append("status = %s")
    params.append(str(f.get("status") or "done"))

    if f.get("dataset"):
        in_or_eq("dataset", f["dataset"])
    if f.get("exclude_dataset"):
        in_or_eq("dataset", f["exclude_dataset"], negate=True)
    if f.get("dataset_prefix"):
        # CoSyn_400k_* 这种成组的前缀，省得把二十个名字全抄一遍。
        prefixes = f["dataset_prefix"]
        if not isinstance(prefixes, (list, tuple)):
            prefixes = [prefixes]
        ors = " OR ".join(["dataset LIKE %s"] * len(prefixes))
        conds.append(f"({ors})")
        params.extend([f"{p}%" for p in prefixes])
    if f.get("lang"):
        in_or_eq("lang_assigned", f["lang"])
    if f.get("source_lang"):
        in_or_eq("source_lang", f["source_lang"])
    if f.get("batch_id"):
        in_or_eq("batch_id", f["batch_id"])

    if f.get("with_images"):
        conds.append("image_count > 0")
    if f.get("no_images"):
        conds.append("image_count = 0")
    if f.get("min_images") is not None:
        conds.append("image_count >= %s")
        params.append(int(f["min_images"]))

    return " AND ".join(conds), params


def fetch_candidates(pool: Any, group: dict[str, Any]) -> dict[str, list[int]]:
    """返回 {桶键: [id,...]}。没配 balance_by 时桶键固定 __all__。

    只取主键，不取正文——``idx_pick (dataset, status, id)`` 是覆盖索引，几百万行
    也就几秒。正文等采样定了再按 id 回表，避免把整个类别的对话都读进内存。
    """

    where, params = build_where(group.get("filter") or {})
    bal = group.get("balance_by") or ("dataset" if group.get("max_share_per_dataset") else None)
    col = f", {bal}" if bal else ""

    def query(cursor: Any) -> list[tuple[Any, ...]]:
        cursor.execute(
            f"SELECT id{col} FROM sample_source WHERE {where} ORDER BY id", params
        )
        return list(cursor.fetchall())

    buckets: dict[str, list[int]] = defaultdict(list)
    for row in pool.run(query):
        buckets[str(row[1]) if bal else "__all__"].append(int(row[0]))
    return buckets


def _quota(
    buckets: dict[str, list[int]], target: int, cap_share: float | None
) -> tuple[dict[str, int], int]:
    """把 target 分到各桶，返回 (每桶条数, 为达标而突破封顶的条数)。

    配了 ``max_share_per_dataset`` 时**按容量注水**，不是按池子大小正比分。
    正比分是错的：它本身就是「大的吃大头」，封顶只削顶不抬底，小数据集会被
    按比例压到几百条，回补又全流回大的那个。实测过一次——封顶写 18%，
    densefusion_1m 实际拿了 89.3%。注水则是先让每家都吃饱到 min(封顶, 存量)，
    谁先到顶谁停，省下的额度匀给还有胃口的。

    只有当「所有桶都吃到封顶仍凑不够 target」时才会突破封顶，这时返回突破量，
    由调用方打警告——保比例和保多样性冲突了，得让人看见，不能默默选一边。
    """

    keys = sorted(buckets)
    if not keys:
        return {}, 0
    if len(keys) == 1:
        return {keys[0]: target}, 0

    if cap_share is not None:
        ceiling = {k: min(max(1, int(target * cap_share)), len(buckets[k])) for k in keys}
    else:
        ceiling = {k: len(buckets[k]) for k in keys}

    # 注水：容量小的先定，省下的额度自动流向容量大的。
    per: dict[str, int] = {}
    remaining = target
    order = sorted(keys, key=lambda k: ceiling[k])
    for index, key in enumerate(order):
        share = remaining // (len(order) - index)
        per[key] = min(ceiling[key], share)
        remaining -= per[key]

    if remaining <= 0:
        return per, 0

    # 封顶之内凑不够。按剩余存量回补，并把突破量报给调用方。
    over = 0
    for key in sorted(keys, key=lambda k: len(buckets[k]) - per[k], reverse=True):
        if remaining <= 0:
            break
        room = len(buckets[key]) - per[key]
        add = min(room, remaining)
        per[key] += add
        remaining -= add
        if cap_share is not None:
            over += add
    return per, over


def pick(
    buckets: dict[str, list[int]],
    target: int,
    rng: random.Random,
    shortfall: str,
    shuffle: bool,
    name: str,
    cap_share: float | None,
) -> tuple[list[int], int, int, int, dict[str, int]]:
    avail = sum(len(v) for v in buckets.values())
    if avail == 0 or target <= 0:
        return [], 0, avail, 0, {}
    if target > avail:
        if shortfall == "error":
            sys.exit(f"[FATAL] 组 {name} 只有 {avail} 条，要不到 {target} 条")
        if shortfall != "repeat":
            target = min(target, avail)

    per, over_cap = _quota(buckets, target, cap_share)

    if shortfall == "repeat":
        # 注水的上限就是各桶存量，所以它永远凑不满 target。缺口按各桶已分到的
        # 份额**按比例**补重复，而不是全压在某一个桶上——否则上采样会把一个
        # 数据集重复几十遍，过拟合风险全集中在它身上。
        gap = target - sum(per.values())
        if gap > 0:
            base = sum(per.values()) or 1
            order = sorted(per, key=lambda k: per[k], reverse=True)
            extra = {k: gap * per[k] // base for k in order}
            # 整除会留下余数，轮着补掉，否则每组都差几千条凑不齐 target。
            left = gap - sum(extra.values())
            index = 0
            while left > 0:
                extra[order[index % len(order)]] += 1
                left -= 1
                index += 1
            for key in order:
                per[key] += extra[key]

    out: list[int] = []
    for key in sorted(buckets):
        ids = buckets[key]
        n = per.get(key, 0)
        if n <= 0:
            continue
        if n >= len(ids):
            out.extend(ids)
            if n > len(ids):
                out.extend(rng.choice(ids) for _ in range(n - len(ids)))  # repeat
        elif shuffle:
            # 只要 n 条就别洗整份：几百万 id 洗一遍纯属浪费，sample 等价且便宜。
            out.extend(rng.sample(ids, n))
        else:
            out.extend(ids[:n])
    return out, len(out), avail, over_cap, per


def fetch_rows(pool: Any, ids: list[int], chunk: int = 2000):
    """按 id 回表取正文，产出和 db-export 完全一样的 ShareGPT 记录。

    中文样本取译文，英文样本取原文——和 iter_export_records 的口径保持一致，
    两条路导出的东西必须能对得上，否则同一批数据两种写法会得到不同结果。
    """

    from finevision_to_sharegpt.db.mysql_ledger import _load_json

    for start in range(0, len(ids), chunk):
        part = ids[start : start + chunk]
        uniq = list(dict.fromkeys(part))
        ph = ",".join(["%s"] * len(uniq))

        def query(cursor: Any, ph: str = ph, uniq: list[int] = uniq) -> list[tuple[Any, ...]]:
            cursor.execute(
                "SELECT s.id, s.sample_id, s.image_paths, s.conversations, s.lang_assigned, "
                "       (SELECT t.conversations FROM sample_translation t "
                "         WHERE t.source_id = s.id ORDER BY t.created_at DESC, t.id DESC LIMIT 1) "
                f"FROM sample_source s WHERE s.id IN ({ph})",
                uniq,
            )
            return list(cursor.fetchall())

        rows = {int(r[0]): r for r in pool.run(query)}
        for sid in part:  # 保持采样顺序，也支持 repeat 模式的重复
            row = rows.get(sid)
            if row is None:
                continue
            translated = _load_json(row[5])
            conversations = translated if row[4] == "zh" and translated else _load_json(row[3])
            if not conversations:
                continue
            yield {
                "id": row[1],
                "images": _load_json(row[2]) or [],
                "conversations": conversations,
            }


def main() -> int:
    ap = argparse.ArgumentParser(description="按配比从通用账本导出训练集")
    ap.add_argument("--config", required=True, help="yaml 或 json 配比文件")
    ap.add_argument("--out", default=None, help="覆盖配置里的 output")
    ap.add_argument("--seed", type=int, default=None, help="覆盖配置里的 seed")
    ap.add_argument("--dry-run", action="store_true", help="只算配比，不写文件")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    cfg_hash = hashlib.sha256(cfg_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:12]

    groups = cfg.get("groups") or []
    if not groups:
        sys.exit("[FATAL] 配置里没有 groups")
    if not cfg.get("mysql"):
        sys.exit("[FATAL] 配置里没有 mysql 段")
    seed = args.seed if args.seed is not None else cfg.get("seed", 42)
    shuffle = cfg.get("shuffle", True)
    shortfall = cfg.get("on_shortfall", "scale")
    if shortfall not in SHORTFALL_MODES:
        sys.exit(f"[FATAL] on_shortfall 只能是 {SHORTFALL_MODES}")
    out_path = args.out or cfg.get("output")
    split_by_group = cfg.get("split_by_group", False)
    if not args.dry_run and not out_path:
        sys.exit("[FATAL] 需要 output（配置里写或 --out 传）")

    rng = random.Random(seed)
    mysql = load_mysql_config(cfg["mysql"])
    if "${" in (mysql.password or ""):
        sys.exit("[FATAL] 密码没展开，先 export FV_MYSQL_PASSWORD=...")
    # ensure_schema=False：导出是只读操作，绝不在几千万行的表上跑 DDL。
    ledger = MySQLLedger(mysql, ensure_schema=False)
    pool = ledger.pool
    t0 = time.time()

    try:
        print("[1/3] 统计各组候选量 ...")
        cands: dict[str, dict[str, list[int]]] = {}
        avail: dict[str, int] = {}
        for g in groups:
            buckets = fetch_candidates(pool, g)
            cands[g["name"]] = buckets
            avail[g["name"]] = sum(len(v) for v in buckets.values())
            note = ""
            if g.get("max_share_per_dataset"):
                note = f"  (分 {len(buckets)} 个数据集，单个封顶 {g['max_share_per_dataset']:.0%})"
            elif g.get("balance_by"):
                note = f"  (按 {g['balance_by']} 分 {len(buckets)} 桶均分)"
            print(f"    {g['name']:<22} 候选 {avail[g['name']]:>9}{note}")

        print("[2/3] 算目标条数 ...")
        explicit = {g["name"]: int(g["count"]) for g in groups if g.get("count")}
        weighted = [g for g in groups if not g.get("count") and g.get("weight")]
        total = cfg.get("total")
        targets: dict[str, int] = dict(explicit)

        if weighted:
            wsum = sum(float(g["weight"]) for g in weighted)
            if total:
                quota = max(int(total) - sum(explicit.values()), 0)
            else:
                ratio = min(avail[g["name"]] / float(g["weight"]) for g in weighted)
                quota = int(ratio * wsum)
                print(f"    未指定 total，按不失真上限推得权重池 {quota}")
            for g in weighted:
                targets[g["name"]] = int(quota * float(g["weight"]) / wsum)

            if shortfall == "scale":
                factor = 1.0
                for g in weighted:
                    n, a = targets[g["name"]], avail[g["name"]]
                    if n > a and n > 0:
                        factor = min(factor, a / n)
                if factor < 1.0:
                    print(f"    [WARN] 有组数据不足，等比缩小到 {factor:.3f} 以保住比例")
                    for g in weighted:
                        targets[g["name"]] = int(targets[g["name"]] * factor)

        for nm, n in explicit.items():
            if n > avail[nm]:
                print(f"    [WARN] {nm} 要 {n} 条但只有 {avail[nm]} 条（{shortfall} 模式处理）")

        print("[3/3] 采样 ...")
        picked: dict[str, list[int]] = {}
        actual: dict[str, int] = {}
        over_caps: dict[str, int] = {}
        for g in groups:
            nm = g["name"]
            ids, got, av, over, per = pick(
                cands[nm], targets.get(nm, 0), rng, shortfall, shuffle, nm,
                g.get("max_share_per_dataset"),
            )
            picked[nm] = ids
            actual[nm] = got
            over_caps[nm] = over
            pct = got / av * 100 if av else 0
            print(f"    {nm:<22} 目标 {targets.get(nm, 0):>8}  实抽 {got:>8}  ({pct:.1f}% of {av})")
            # 组内构成必须能看见：比例对不代表没被一个数据集吃掉。
            if per and len(per) > 1 and got:
                top = sorted(per.items(), key=lambda kv: kv[1], reverse=True)
                shown = "  ".join(f"{k}={v}({v / got * 100:.0f}%)" for k, v in top[:4])
                more = f"  …另 {len(top) - 4} 个" if len(top) > 4 else ""
                print(f"      占比最高: {shown}{more}")
            if shortfall == "repeat" and av and got > av:
                print(
                    f"      [WARN] repeat 模式重复率 {got / av:.1f}x"
                    f"（{av} 条不重复的样本撑出 {got} 条）。倍数越高越容易过拟合。"
                )
            cap = g.get("max_share_per_dataset")
            if over and cap:
                worst = max(per.values()) / got if got else 0
                print(
                    f"      [WARN] 封顶 {cap:.0%} 装不下 {targets.get(nm, 0)} 条，"
                    f"突破 {over} 条，最大单集实占 {worst:.0%}。"
                )
                print("             要么调低本组 weight/total，要么放宽封顶——现在是保了比例牺牲了多样性。")

        grand = sum(actual.values())
        print(f"    合计 {grand}")
        if grand:
            print("    实际比例: " + "  ".join(f"{n}={actual[n] / grand * 100:.1f}%" for n in actual))

        if args.dry_run:
            print(f"\n[dry-run] 没有写文件。耗时 {time.time() - t0:.0f}s")
            return 0

        print("写出 ...")
        out_p = Path(out_path)
        stat: Counter[str] = Counter()
        if split_by_group:
            out_p.mkdir(parents=True, exist_ok=True)
            for nm, ids in picked.items():
                with (out_p / f"{nm}.jsonl").open("w", encoding="utf-8") as fh:
                    for record in fetch_rows(pool, ids):
                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                        stat[nm] += 1
                print(f"    {nm}.jsonl  {stat[nm]} 条")
        else:
            out_p.parent.mkdir(parents=True, exist_ok=True)
            order = [(n, i) for n, ids in picked.items() for i in ids]
            if shuffle:
                rng.shuffle(order)
            by_group: dict[str, list[int]] = defaultdict(list)
            for n, i in order:
                by_group[n].append(i)
            cache: dict[tuple[str, int], str] = {}
            for n, ids in by_group.items():
                for sid, record in zip(ids, fetch_rows(pool, ids)):
                    cache.setdefault((n, sid), json.dumps(record, ensure_ascii=False))
            with out_p.open("w", encoding="utf-8") as fh:
                for n, i in order:
                    line = cache.get((n, i))
                    if line:
                        fh.write(line + "\n")
                        stat[n] += 1
            print(f"    {out_p}  {sum(stat.values())} 条")

        manifest = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "config_file": str(cfg_path),
            "config_sha256_12": cfg_hash,
            "seed": seed,
            "shuffle": shuffle,
            "on_shortfall": shortfall,
            "output": str(out_p),
            "groups": [
                {
                    "name": g["name"],
                    "filter": g.get("filter"),
                    "weight": g.get("weight"),
                    "count": g.get("count"),
                    "balance_by": g.get("balance_by"),
                    "max_share_per_dataset": g.get("max_share_per_dataset"),
                    "available": avail[g["name"]],
                    "target": targets.get(g["name"], 0),
                    "written": stat[g["name"]],
                    "over_cap": over_caps.get(g["name"], 0),
                }
                for g in groups
            ],
            "total_written": sum(stat.values()),
        }
        mpath = (
            out_p / "manifest.json"
            if split_by_group
            else out_p.with_suffix(out_p.suffix + ".manifest.json")
        )
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    manifest -> {mpath}")
        print(f"总耗时 {time.time() - t0:.0f}s")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
