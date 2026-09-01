# 双 Claude 协作约定

这个仓库有两个 Claude 会话在改。上一次没对齐造成的实际损失：

- Claude 1 把连接池从「每线程一条」改成有界共享，Claude 2 的手册里
  「必须 `SET GLOBAL max_connections = 500`」因此失效，用户照着执行卡住。
- Claude 2 打开了 `store_conversations`，Claude 1 不知道，仍按关着的前提
  给用户建议，判断反了。

两次都不是代码冲突，是**行为契约变了而对方不知道**。所以约定的重点不在
「谁写了什么」，而在「我的改动会让对方的哪句话变成错的」。

## 分工

| | 负责 |
| --- | --- |
| **Claude 1** | 数据库与入库：账本、连接池、schema、db-* 命令、MySQL 部署 |
| **Claude 2** | 流水线与数据逻辑：抽取、解析、翻译、抽样规划、运行文档 |

## 文件归属

**Claude 1 独占**

```
src/finevision_to_sharegpt/db/**
src/finevision_to_sharegpt/db_commands.py
scripts/setup_local_mysql.sh
scripts/db_inventory.py
tests/test_db_ledger.py, tests/test_db_integration.py
configs/*.json 里的 mysql 段
```

**Claude 2 独占**

```
src/finevision_to_sharegpt/{zip_pipeline,translator,qwen_client,sample_parser,
  backend_pool,parquet_reader,dataset_registry,archive,image_store,json_io,
  validator,translation_job,concurrency}.py
scripts/{plan_sampling,split_scan_configs,register_datasets,survey_roots,
  estimate_space,check_backends}.py
configs/*.json 里的非 mysql 部分、抽样计划
docs/运行手册-*.md, docs/运行说明.md
```

**共有 —— 改之前必须在自己日志里声明**

```
src/finevision_to_sharegpt/config_loader.py    两边都要加配置字段
src/finevision_to_sharegpt/cli.py              两边都要注册子命令
src/finevision_to_sharegpt/zip_pipeline.py     账本生命周期在这里，Claude 1 也会动
configs/*.json                                 同一个文件两边都改
```

## 每次动手前

```bash
git pull
tail -60 docs/协作/claude1-日志.md    # 或 claude2
git log --oneline -10
```

三样都看。只看 git log 不够——日志里才有「这次改动让你的什么假设失效了」。

## 每次提交后

在自己的日志文件顶部追加一条，**新的在上**。格式：

```markdown
## <日期> · <commit sha> · <一句话说清做了什么>

**动了**：文件清单
**为什么**：一两句
**对另一侧的影响**：没有 / 或具体写明哪个假设、哪份文档、哪个配置字段的含义变了
```

第三项是这份约定存在的理由。填「没有」之前先想一遍：
对方的文档里有没有哪句话，因为我这次改动而变成错的？

## 边界不清时

不要猜，也不要顺手改到对方的文件里。在自己日志里写一条
`**需要对方处理**：……`，用户会转达。
