# Claude 1 · 数据库与入库 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 2026-09-01 · 3474738 · 手册去掉已失效的 max_connections 步骤，摘掉未填 key 的后端

**动了**：`docs/运行手册-500万.md`、`configs/backend_config.json`、`configs/translate_5m.json`

**为什么**：手册阶段 2 让用户执行 `SET GLOBAL max_connections = 500`，理由是
「翻译要 4×64 = 256 路」。连接池改造后这个前提不成立了，而且这条命令用户
执行不了——`fv` 只有库级权限没有 SUPER，换 root 又因为 `--initialize-insecure`
建库时 root 是空密码，加 `-p` 反而登录失败。用户实际卡在这里。

顺带移除 `remote-zjlab`（`api_key` 还是占位符，留着会连续失败 20 次才被
`disable_backend_after_failures` 停用，白白浪费开跑后的头一批请求），
`translate_5m.json` 显式写出 `pool_size: 8` 不依赖默认值。

**对另一侧的影响**：**改到了 Claude 2 的手册和后端配置**。
- 手册阶段 2 少了一步，验收标准不变。
- `backend_config.json` 从 5 个后端变成 4 个，总并发 272 → 256。拿到远端 key
  后要加回来。
- 任何提到「需要调 max_connections」的文字都可以删。

---

## 2026-09-01 · bf88214 · 连接池改为有界共享

**动了**：`db/pool.py`、`db/config.py`、`configs/*.json` 的 mysql 段、`docs/运行说明.md`

**为什么**：原本用 `threading.local()` 给每个工作线程一条常驻连接，服务端需要的
连接数等于翻译总并发。但这些线程绝大部分时间在等模型，数据库调用短且已攒批。
Claude 2 把并发设到 272，按旧设计必然打满默认的 151——用户撞的
"Too many connections" 根源在此。

改为固定上限的共享池，新增 `mysql.pool_size`（默认 8）和
`checkout_timeout_seconds`（默认 60）。真实 MySQL 8.4 实测：128 工作线程、
600 次调用，服务端峰值连接严格等于 `pool_size`；8 与 16 吞吐无差异。

**对另一侧的影响**：**这是让手册那句话失效的改动**。
- 服务端连接数 = `并发进程数 × pool_size`，与翻译并发无关。
- 删掉了 `ConnectionPool.connection()` 和 `_discard_local()`。已确认
  `scripts/db_inventory.py` 只用 `ConnectionPool(cfg)` 和 `pool.run()`，未受影响。
- 后续要调翻译并发，不必再动数据库任何设置。

---

## 2026-09-01 · 2515f24 · 修复账本连接泄漏，连接失败自解释

**动了**：`db/pool.py`、`zip_pipeline.py`（共有文件）、`tests/test_db_ledger.py`、
`tests/test_zip_pipeline_ledger.py`

**为什么**：`_prepare_zip_run` 建立 ledger 时就开了 MySQL 连接，却要到几十行
之后才进入负责关闭它的 `with` 块。中间任何异常或 Ctrl-C 都漏一条连接，而
`wait_timeout` 默认八小时，反复重试足以打满上限。

**对另一侧的影响**：**动了 `zip_pipeline.py` 这个共有文件**。
`run_translate_zips` 拆成了薄壳 + `_run_translate_zips`，壳负责 `with ledger:`，
主体逻辑一行没改。Claude 2 之后改这个函数请在 `_run_translate_zips` 里改，
别把 ledger 的获取挪出 `with`。

---

## 2026-09-01 · 3fbcfee · db-scan 支持断点续传（`for_ingest`）

**动了**：`db/ledger.py`、`db/mysql_ledger.py`、`zip_pipeline.py`（共有文件）

**为什么**：`scan_plan` 把 `pending` 视为未完成而把扫描起点拉回 0——这对消费
路径是对的（`pending` 正是它要找的），但对灌库是灾难：几百个数据集灌到一半
中断，重启等于从头再来。新增 `for_ingest`，灌库时从水位线继续。

**对另一侧的影响**：**db-scan 的行为变了**。不清 `dataset_cursor` 的话
`db-scan` 一行都不会重读。Claude 2 的手册阶段 5 已经正确写明了这一点
（`DELETE FROM dataset_cursor;` 是补扫的必要前置），无需改动。

---

## 待办 / 需要对方确认

- **随机抽样**：底层 `iter_parquet_rows_at()`（按行号只读对应 row group）已实现
  并测过，但「从库里随机选行 → 喂给流水线」这段没接。在 `chinese_ratio: 1.0`
  的全翻模式下暂时用不上，等需要按配比抽子集时再说。归属上跨两边，动手前先议。
