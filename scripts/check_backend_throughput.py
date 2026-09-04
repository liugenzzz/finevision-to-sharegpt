#!/usr/bin/env python3
"""翻译跑着的时候查：五个后端是不是均摊，有没有谁掉队或已经被摘掉。

只读，不写库，不影响正在跑的任务。凭据从任务配置里读，不用重敲。

    python check_backends_live.py configs/translate_5m.json
    python check_backends_live.py configs/translate_5m.json --window 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 脚本放哪都行：优先按自己的位置找 src，找不到就按当前目录找。
for _candidate in (Path(__file__).resolve().parent / "src", Path.cwd() / "src"):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
        break

from finevision_to_sharegpt.config_loader import load_zip_task_config  # noqa: E402
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger  # noqa: E402


def fmt_ago(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}秒前"
    if seconds < 5400:
        return f"{seconds // 60}分钟前"
    return f"{seconds // 3600}小时前"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--window", type=int, default=5, help="「最近」窗口，单位分钟")
    args = ap.parse_args()

    config = load_zip_task_config(args.config)
    if config.mysql is None:
        print("这份配置没有 mysql 段", file=sys.stderr)
        return 1
    if "${" in (config.mysql.password or ""):
        print("密码没展开，先 export FV_MYSQL_PASSWORD=...", file=sys.stderr)
        return 1

    # ensure_schema=False：绝不能在正在跑的库上执行 DDL。默认构造会去补列，
    # 对一张几千万行的表是灾难。这个脚本只读，一个字都不写。
    ledger = MySQLLedger(config.mysql, ensure_schema=False)
    w = args.window
    try:
        def q(cursor):
            cursor.execute(
                "SELECT backend_name, COUNT(*), ROUND(AVG(latency_ms)), MAX(latency_ms),"
                "       TIMESTAMPDIFF(SECOND, MAX(created_at), NOW()),"
                "       SUM(created_at >= NOW() - INTERVAL %s MINUTE)"
                "  FROM sample_translation GROUP BY backend_name ORDER BY 2 DESC",
                (w,),
            )
            return list(cursor.fetchall())

        def status(cursor):
            cursor.execute("SELECT status, COUNT(*) FROM sample_source GROUP BY status")
            return list(cursor.fetchall())

        rows = ledger.pool.run(q)
        states = ledger.pool.run(status)
    finally:
        ledger.close()

    if not rows:
        print("sample_translation 里还没有行——翻译要么没在跑，要么还没有一条落库。")
        return 0

    print(f"== 后端产出对比（窗口 {w} 分钟）\n")
    head = f"  {'后端':<14}{'累计':>10}{'最近'+str(w)+'分钟':>12}{'条/秒':>9}{'平均ms':>9}{'最慢ms':>10}   最后一条"
    print(head)
    print("  " + "-" * (len(head) - 2))
    recent_total = 0
    for name, total, avg_ms, max_ms, ago, recent in rows:
        recent = int(recent or 0)
        recent_total += recent
        rate = recent / (w * 60)
        print(
            f"  {str(name or '(未记录)'):<14}{int(total):>10}{recent:>12}{rate:>9.2f}"
            f"{('—' if avg_ms is None else int(avg_ms)):>9}"
            f"{('—' if max_ms is None else int(max_ms)):>10}   {fmt_ago(ago)}"
        )
    print(f"\n  合计最近速率  {recent_total / (w * 60):.2f} 条/秒")

    print("\n== 源行状态")
    for st, n in states:
        print(f"  {st:<10}{int(n):>12}")

    print("\n== 怎么读")
    # backend_name 为 NULL 的是 db-restore 从 JSONL 灌回来的行（restore 调
    # record_translation 时 backend_name 传 None），不是某个停掉的后端。
    # 把它算成「已被摘掉」会误导人，这里单列。
    restored = [r for r in rows if r[0] is None]
    real = [r for r in rows if r[0] is not None]
    live = [r for r in real if int(r[5] or 0) > 0]
    dead = [r for r in real if int(r[5] or 0) == 0]
    if restored:
        n = sum(int(r[1]) for r in restored)
        print(f"  （未记录后端的 {n} 条是 db-restore 灌回来的历史产出，不是掉线的后端。）")
    if dead:
        print("  以下后端在窗口内一条都没产出——已被摘掉，或它的线程全卡在超时里：")
        for name, total, _a, _m, ago, _r in dead:
            print(f"    {name}（累计 {int(total)} 条，最后一条 {fmt_ago(ago)}）")
    if len(live) > 1:
        counts = sorted(int(r[5]) for r in live)
        if counts[-1] >= counts[0] * 3:
            print("  活着的后端之间产出差 3 倍以上，慢的那个在拖后腿（看它的平均ms）。")
        else:
            print("  活着的后端产出接近，负载均衡本身没问题。")
    # 一条样本的耗时上限 = 单次调用超时 + 回退总预算。两个值都在后端配置里，
    # 写死会随配置漂移（request_timeout 就从 300 改成过 60）。
    ceiling = None
    try:
        from finevision_to_sharegpt.config_loader import load_backend_config

        bc = load_backend_config(config.backend_config)
        ceiling = bc.request_timeout + getattr(bc, "fallback_budget_seconds", 0)
        print(f"\n  样本耗时上限 {ceiling}s = request_timeout {bc.request_timeout}s"
              f" + fallback_budget {getattr(bc, 'fallback_budget_seconds', 0)}s")
    except Exception:
        pass

    print()
    for name, _total, avg_ms, max_ms, _ago, recent in live:
        if not avg_ms:
            continue
        busy = (int(recent) / (w * 60)) * (int(avg_ms) / 1000)
        print(f"  {name}：平均 {int(avg_ms)/1000:.1f}s × 实测 {int(recent)/(w*60):.2f} 条/秒"
              f" → 有效并发约 {busy:.0f}。拿它比 backend_config 里的 concurrency：")
        print("      差得远 = 线程卡在长请求里没退出来，不是模型慢。")
        if max_ms and ceiling and int(max_ms) > ceiling * 1000 * 1.5:
            print(f"      最慢 {int(max_ms)/1000:.0f}s 超出上限 {ceiling}s"
                  f"（request_timeout + fallback_budget_seconds）。"
                  f"要么是加预算之前的老数据，要么有条路径绕过了预算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
