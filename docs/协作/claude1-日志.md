# Claude 1 · 数据库与入库 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 2026-09-02 · db-restore 从 JSONL 灌回账本；顺带修掉 MySQL 模式下续跑超配额

**动了**：`db_commands.py`、`db/ledger.py`、`db/mysql_ledger.py`、
`cli.py`（共有文件，按约定声明）、`zip_pipeline.py`（共有文件，按约定声明）、
`tests/test_db_integration.py`、`tests/test_cli.py`

**为什么**：回应「待 Claude 1 处理」第二条。用户确认翻译后续还会跑，所以那
36.7 万条必须回到账本——否则续跑时 `resume` 一条都跳不过去，23.7 小时的 GPU
产出会重做一遍，还会往同一个 JSONL 里追加重复记录。

新增 `db-restore`：按注册表逐个数据集读 `<out>/<dataset>/{train,raw}.jsonl`，
还原 `sample_source`（`done`）与 `sample_translation`，`--dry-run` 只统计。

**关键点：只补 `done` 行是不够的。** `is_consumed` 对 `row_index >= gap_end`
直接返回未消费，而 `gap_end` 来自 `dataset_cursor`。不重建水位线，恢复完照样
全部重翻。有一个测试专门去掉水位线来证明这一点。

**写这个功能时发现了一个更要紧的 bug，不是恢复独有的。** 你把 `limit` 改成
总量语义、把 `skipped` 计入配额之后，MySQL 路径仍然会超额：水位线让已完成的行
**根本没被读到**，`skipped` 因此不增长，判断被绕过。实测 `limit=5` 的 8 行
数据集，第一轮 5 条、第二轮又 3 条，合计 8。用户的 500 万按每数据集配额跑，
**每次重启都会在每个数据集上超额**，十几天里必然中断多次。

修法：`ScanPlan` 增加 `skipped_before`，`scan_plan` 数出起点之下 `done` 的行数
交给流水线补进配额。只数 `done`——`rejected` 没有产出任何样本，不该占配额。
缺口内的行照旧逐条计数，两者相加不重复。

**对另一侧的影响**：**三点。**

① **`zip_pipeline.py` 我加了三行**：拿到 `plan` 之后把 `plan.skipped_before`
累加进 `totals["skipped"]` 和 `dataset_stats["skipped"]`。你的 `limit_reached`
一个字没动，只是现在它能看见那些从未被读到的行了。

② **`ScanPlan` 多了一个字段** `skipped_before`，默认 0。`JsonlLedger` 返回默认
值即可——文件模式每行都会读到，`skipped` 本来就是准的，补报反而会重复计数。

③ **我改了一个旧测试** `test_two_runs_with_separate_outputs_never_repeat_a_sample`。
它是我早先写的，编码的是旧的「本轮增量」语义（两轮各 limit=4，期望各写 4 条）。
你改成总量语义之后它本该失败，只是 MySQL 路径的绕过让它一直蒙混过关；我修完
才暴露。现在改成「同样配额再跑写 0 条，调大到 8 才继续写 4 条」，它原本要证明的
「换输出目录不重复抽样」保住了。

**还剩一个你提过的边角我没解决**：缺口内 `rejected` 的行仍会计入配额（走
`consumed_ids` → `skipped`），而起点之下的 `rejected` 现在不计。拒绝率高的
数据集续跑后配额行为仍不一致。彻底修要让流水线能区分「跳过因为已完成」和
「跳过因为已拒绝」，得动 `is_consumed` 的契约。眼下 `limit_per_dataset` 为
`None` 的全量灌库不受影响，先记在这里。

**验证**：251 passed（连真 MySQL 8.4）/ 236 passed + 15 skipped（无库）。
新增 5 个集成用例：恢复后续跑跳过、抹掉水位线就白做、dry-run 不写库、
续跑不超配额、调大配额能继续。

---

## 2026-09-02 · `setup_local_mysql.sh` 可装进已有环境，BASE 不再有默认值

**动了**：`scripts/setup_local_mysql.sh`

**为什么**：回应「待 Claude 1 处理」的第一条。两个问题一起修：

① **二进制装进已有的 conda 环境。** 原来只有 `conda create -p`，对着已存在的
prefix 会直接报 `prefix already exists` 退出，所以 `FV_MYSQL_ENV` 指向 fv 环境
等于让脚本挂掉。现在判断 prefix 存不存在，存在就走 `conda install -p`。
用户要的「`conda activate fv` 之后 `mysql` 直接可用」由此成立，也就不用再先
手动跑一条 `conda install`。

② **BASE 不再有默认值**，必须显式给，提示里点明「放在持久卷上」。原来的默认
`/mnt/fv/mysql` 把 datadir 放在了重启就清空的卷上，用户因此丢了 2524 万行账本。
另加一个持久性检查：代码所在的卷经历过重启仍在，所以拿它作参照，BASE 落在
不同设备上就打印警告。这是启发式，不是保证，措辞上说明了这一点。

**实测**：造一个只有 python 的 conda 环境模拟 fv，脚本走 `conda install` 分支
装入，随后 `mysqld` / `mysql` 出现在该环境的 bin 下、`mysql --version` 为
8.4.2、python 3.11 未被挤掉，建库建号并连上成功。另测了二进制已存在时跳过安装
的分支。

**对另一侧的影响**：**用法变了，文档里凡是写 `setup_local_mysql.sh` 的都要改。**
- BASE 现在是必填参数，不给会退出并打印用法。
- 推荐用法变成 `FV_MYSQL_ENV=<fv环境> bash scripts/setup_local_mysql.sh <持久盘上的目录>`。
- 之前你给用户的绕法（先手动 `conda install -p`）不再需要，但仍然有效。

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
