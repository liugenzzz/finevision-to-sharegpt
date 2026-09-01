# finevision-to-sharegpt

把 FineVision 风格的 parquet/zip 数据集转成 LLaMA-Factory 的 ShareGPT 多模态
训练数据，可选用 MySQL 做消费账本实现跨任务增量抽取。

## 开工前必做

**这个仓库有两个 Claude 会话同时在改。** 动手前三样都看：

```bash
git pull
cat docs/协作/README.md                  # 分工与文件归属
tail -60 docs/协作/claude1-日志.md        # 和 claude2-日志.md
```

只看 `git log` 不够——日志里才有「这次改动让对方的什么假设失效了」。
提交后在自己的日志顶部追加一条，重点填「对另一侧的影响」。

## 跑起来

```bash
python -m pytest -q                      # 无数据库时会跳过集成用例
python -m ruff check src tests scripts --select F,E9
```

集成用例要连真库，设了才跑：

```bash
export FV_TEST_MYSQL='{"host":"127.0.0.1","port":3306,"user":"fv","password":"...","database":"finevision"}'
```

本地起一个私有 MySQL 8.4（无需 root 权限，全在一个可写目录下）：

```bash
export FV_MYSQL_PASSWORD='...'
bash scripts/setup_local_mysql.sh /mnt/fv/mysql
```

## 不能违反的语义

这几条都是踩过坑才定下来的，改之前先读对应的测试。

- **账本连接的生命周期**：`_prepare_zip_run` 返回的 ledger 已经握着一条 MySQL
  连接，必须立刻进入 `with ledger:`。中间任何异常都会漏一条连接，而
  `wait_timeout` 默认八小时。见 `test_the_ledger_is_closed_even_if_setup_fails_afterwards`。
- **连接数按 `pool_size` 算，不按并发算**：连接池是有界共享的，272 路翻译并发
  在服务端也只占 `pool_size` 条。别再按并发去估 `max_connections`。
- **`scan_plan` 有两套语义**：消费路径要找 `pending` 行，所以起点会回退到最早
  的未完成行；灌库路径（`for_ingest=True`）从水位线继续。想让 `db-scan` 重扫
  必须先 `DELETE FROM dataset_cursor`。
- **`store_conversations`**：关掉后账本只存 id/图片路径/状态，英文原文留在
  parquet 里。**关着跑完的行不会回填**，`db-export` 也就导不出英文样本。改这个
  开关前先确认另一侧的文档是怎么写的。
- **图片不进库**，只存 `images/<数据集>/<sha256>.<ext>` 相对路径。文件名由内容
  决定，所以 `--no-images` 扫描记下的路径和之后真正落盘的路径一致。

## 两条经验

- **不要从截图或聊天记录里转录数据集名和路径。** 挂载点里的 `0/O`、`1/l` 在等宽
  字体下分不清，这个错误在本项目里造成过两次返工。名字一律从账本读：
  `python scripts/plan_sampling.py --config <cfg> --dump`。
- **外推容量要用多个样本。** 曾用单个数据集的 `bytes_per_row` 外推整棵树，
  结果差了 2.7 倍（估 12 GB，实际 33 GB）。

## 代码风格

中文注释与提交信息。注释只写「为什么」，不复述代码做了什么。
测试名说明被验证的行为，不是被调用的函数。
