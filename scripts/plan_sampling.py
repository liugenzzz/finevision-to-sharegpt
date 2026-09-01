#!/usr/bin/env python3
"""Turn a category quota plan into a task config with per-dataset limits.

The pipeline can already cap each dataset individually, so a stratified mix is
a planning problem, not a pipeline change: decide how many samples each
dataset should contribute, then write those numbers into the config.

Dataset names come from the ledger, never from a hand-typed list, and every
dataset that no rule matched is reported rather than silently dropped.

    python scripts/plan_sampling.py --config configs/db_scan.json --dump
    python scripts/plan_sampling.py --config configs/db_scan.json \\
        --plan configs/sampling_plan.json -o configs/translate_pretrain.json

``--counts`` takes the same numbers from a text file instead, so a mix can be
planned before any ingest has happened — a full db-scan of millions of rows is
a long wait to sit through just to find out a share is unreachable.

    python scripts/plan_sampling.py --counts fv_counts.txt \\
        --config configs/translate_zips.json \\
        --plan configs/sampling_plan_8m.json -o configs/translate_8m.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finevision_to_sharegpt.db_commands import run_db_status  # noqa: E402


def available_rows(config_path: str, statuses: tuple[str, ...]) -> dict[str, int]:
    """Rows per dataset that a translation run could still claim."""

    counts: dict[str, int] = {}
    for row in run_db_status(config_path)["rows"]:
        if row["status"] in statuses:
            counts[row["dataset"]] = counts.get(row["dataset"], 0) + row["count"]
    return counts


def read_counts(path: Path | str) -> dict[str, int]:
    """Read ``<count> <dataset>`` lines, as ``--dump`` and ``huggingface`` print them.

    Counts keep their thousands separators and names keep their parentheses:
    the file is meant to be pasted straight out of a terminal, not cleaned up
    by hand first.
    """

    counts: dict[str, int] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"{path}:{number}: expected '<count> <dataset>', got {stripped!r}")
        try:
            counts[parts[1].strip()] = int(parts[0].replace(",", "").replace("_", ""))
        except ValueError:
            raise ValueError(f"{path}:{number}: {parts[0]!r} is not a row count") from None
    return counts


def match_datasets(names: list[str], rules: dict) -> list[str]:
    exact = {str(item) for item in rules.get("match") or ()}
    patterns = [re.compile(item, re.IGNORECASE) for item in rules.get("patterns") or ()]
    matched = []
    for name in names:
        if name in exact or any(pattern.search(name) for pattern in patterns):
            matched.append(name)
    return matched


def allocate(
    target: int, pools: dict[str, int], max_share: float | None = None
) -> dict[str, int]:
    """Split ``target`` across datasets, proportional to what each one has.

    A dataset can be smaller than its proportional share, so whatever it
    cannot supply is redistributed over the datasets that still have room.

    ``max_share`` caps any single dataset at that fraction of ``target``.
    Without it one huge set swamps its category: densefusion_1m alone holds
    a million rows and would supply half the caption quota on its own, which
    is a worse mix than the same quota spread over a dozen sources.
    """

    ceilings = dict(pools)
    if max_share is not None:
        cap = max(1, int(target * max_share))
        ceilings = {name: min(room, cap) for name, room in pools.items()}
    pools = ceilings

    allocation = {name: 0 for name in pools}
    remaining = min(target, sum(pools.values()))
    while remaining > 0:
        open_pools = {name: pools[name] - allocation[name] for name in pools}
        open_pools = {name: room for name, room in open_pools.items() if room > 0}
        if not open_pools:
            break
        total_room = sum(open_pools.values())
        handed_out = 0
        for name, room in sorted(open_pools.items()):
            share = min(room, max(1, round(remaining * room / total_room)))
            share = min(share, remaining - handed_out)
            if share <= 0:
                continue
            allocation[name] += share
            handed_out += share
            if handed_out >= remaining:
                break
        if handed_out == 0:
            break
        remaining -= handed_out
    return {name: count for name, count in allocation.items() if count > 0}


def _plan_capped(args: Any, plan: dict, pools: dict[str, int], names: list[str]) -> int:
    """Take min(rows, cap) from every dataset the plan lists.

    Category shares decide a *mix*; this decides *coverage*. Translating a
    dataset whole means any later mix can draw from it by query alone, with no
    second translation pass — the cap only stops one huge set from costing more
    GPU time than any mix would ever use of it.
    """

    requests: list[dict] = []
    claimed: set[str] = set()
    print(f"{'category':<28}{'datasets':>10}{'rows':>12}{'capped':>8}")
    print("-" * 58)
    for category, rules in plan["categories"].items():
        matched = [name for name in match_datasets(names, rules) if name not in claimed]
        claimed.update(matched)
        capped = 0
        rows = 0
        for name in sorted(matched):
            limit = min(pools[name], args.cap)
            if pools[name] > args.cap:
                capped += 1
            rows += limit
            requests.append({"name": name, "limit": limit, "chinese_ratio": args.chinese_ratio})
        print(f"{category:<28}{len(matched):>10}{rows:>12,}{capped:>8}")

    unmapped = [name for name in names if name not in claimed]
    allocated = sum(item["limit"] for item in requests)
    print("-" * 58)
    print(f"{'TOTAL':<28}{len(requests):>10}{allocated:>12,}")
    print(f"\n每集上限 {args.cap:,}；{sum(1 for item in requests if item['limit'] == pools[item['name']])}"
          f"/{len(requests)} 个数据集被完整翻译")
    if unmapped:
        print(f"\n{len(unmapped)} 个数据集不在计划里，已排除:")
        for name in unmapped[:40]:
            print(f"   {pools[name]:>10,}  {name}")

    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base["datasets"] = requests
    base.pop("limit_per_dataset", None)
    base["chinese_ratio"] = args.chinese_ratio
    output = Path(args.output)
    output.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {output} ({len(requests)} datasets, {allocated:,} samples)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="plan a stratified sample across datasets")
    parser.add_argument("--config", required=True, help="task config to base the output on")
    parser.add_argument(
        "--counts",
        help="read dataset row counts from a '<count> <dataset>' file instead of the ledger",
    )
    parser.add_argument(
        "--cap",
        type=int,
        help="translate everything instead of a quota: take min(rows, CAP) from every dataset "
             "the plan lists, ignoring the category shares",
    )
    parser.add_argument(
        "--chinese-ratio",
        type=float,
        default=1.0,
        help="fraction of each dataset translated to Chinese; the rest is kept in English",
    )
    parser.add_argument("--plan", help="category quota plan")
    parser.add_argument("-o", "--output", help="task config to write")
    parser.add_argument("--dump", action="store_true", help="just list datasets and row counts")
    parser.add_argument(
        "--status",
        default="pending,failed",
        help="which ledger statuses count as available (default: pending,failed)",
    )
    args = parser.parse_args(argv)

    if args.counts:
        pools = read_counts(args.counts)
        if not pools:
            print(f"{args.counts} lists no datasets", file=sys.stderr)
            return 1
    else:
        statuses = tuple(item.strip() for item in args.status.split(",") if item.strip())
        pools = available_rows(args.config, statuses)
        if not pools:
            print(f"no rows with status {statuses} in the ledger; run db-scan first", file=sys.stderr)
            return 1

    if args.dump:
        for name in sorted(pools, key=lambda n: -pools[n]):
            print(f"{pools[name]:>10,}  {name}")
        print(f"\n{len(pools)} datasets, {sum(pools.values()):,} rows available")
        return 0

    if not args.plan or not args.output:
        print("--plan and --output are required unless --dump is given", file=sys.stderr)
        return 2

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    total = int(plan["total"])
    names = sorted(pools)
    if args.cap:
        return _plan_capped(args, plan, pools, names)
    requests: list[dict] = []
    claimed: set[str] = set()
    print(f"{'category':<28}{'target':>9}{'allocated':>11}{'datasets':>10}")
    print("-" * 58)
    for category, rules in plan["categories"].items():
        target = int(round(total * float(rules["share"])))
        matched = [name for name in match_datasets(names, rules) if name not in claimed]
        if not matched:
            print(f"{category:<28}{target:>9,}{'0':>11}{'0':>10}   <- NO MATCH")
            continue
        claimed.update(matched)
        allocation = allocate(
            target,
            {name: pools[name] for name in matched},
            max_share=rules.get("max_share_per_dataset"),
        )
        for name, count in sorted(allocation.items()):
            requests.append({"name": name, "limit": count, "chinese_ratio": args.chinese_ratio})
        got = sum(allocation.values())
        flag = "" if got >= target else f"   <- short by {target - got:,}"
        print(f"{category:<28}{target:>9,}{got:>11,}{len(allocation):>10}{flag}")

    unmapped = [name for name in names if name not in claimed]
    allocated = sum(item["limit"] for item in requests)
    print("-" * 58)
    print(f"{'TOTAL':<28}{total:>9,}{allocated:>11,}{len(requests):>10}")
    if unmapped:
        print(f"\n{len(unmapped)} datasets matched no category and are excluded:")
        for name in unmapped[:40]:
            print(f"   {pools[name]:>10,}  {name}")
        if len(unmapped) > 40:
            print(f"   ... and {len(unmapped) - 40} more")

    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base["datasets"] = requests
    base.pop("limit_per_dataset", None)
    base["chinese_ratio"] = args.chinese_ratio
    output = Path(args.output)
    output.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {output} ({len(requests)} datasets, {allocated:,} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
