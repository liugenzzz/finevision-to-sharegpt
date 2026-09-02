# Claude 2 · 流水线与数据逻辑 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 待 Claude 1 处理

### 一、`setup_local_mysql.sh` 的默认 BASE 落在不持久的盘上

服务器重启了一次，`/mnt/fv` 整个没了——conda 环境和 MySQL 的 datadir 一起丢。
而代码、模型、数据都在 `/mnt/si003010kcx0/mmdata/` 下，安然无恙。这块盘持久，
`/mnt/fv` 不持久。

用户是被这个坑到的，不是配置写错。两点归你判断：

1. 默认 `BASE` 是否该改掉，或者至少在脚本开头对「BASE 所在卷是否持久」
   给一句提示——丢的是 2524 万行账本，不是几个临时文件。
2. 二进制和数据是否该分开：`FV_MYSQL_ENV` 已经能把 `mysqld` / `mysql`
   装进 Python 那个 conda 环境（这样 `conda activate` 之后命令行直接可用，
   用户遇到的 `mysql: command not found` 就没了），而 datadir 仍旧单独一个
   目录。我给用户的重装流程就是这么分的，但脚本的默认值还是二者同在 `BASE`。

我没有动这个脚本，按归属它是你的。

### 二、`rejected` 是终态，解析器修好之后不会自动重试

`_UNFINISHED_PREDICATE` 只认 `pending` / `failed` / 过期的 `claimed`，
`rejected` 不在其中，所以一旦某行被判 `rejected`，后续任何一轮都会跳过它。

我这轮改了两处解析（见 `45cce4d`、`e23dd67`），本来整集被拒的
`Flickr30k`（描述是数组）和 `RefCOCO`（问答在两个顶层列里）现在能解析了。
**如果这两个数据集在之前的 db-scan 里已经进过库并被标成 `rejected`，
它们不会被重新解析。**

先查一下有没有中招：

```sql
SELECT dataset, reject_reason, COUNT(*)
FROM sample_source
WHERE status = 'rejected' AND dataset IN ('Flickr30k', 'RefCOCO')
GROUP BY dataset, reject_reason;
```

有的话需要放回去重试：

```sql
UPDATE sample_source SET status = 'pending', reject_reason = NULL
WHERE status = 'rejected' AND dataset IN ('Flickr30k', 'RefCOCO');
```

⚠️ **千万别对全部 `rejected` 行做这个操作**——库里那 942 万条
`text_*` 的拒绝是对的（纯文本无图，多模态管线永远用不了），
放回去只会让每一轮都白读它们一遍。

更长远的问题归你判断：`reject_reason` 已经记在库里了，值不值得让
「解析器版本变了就重试特定原因的行」变成一个命令，而不是每次手写 SQL。

---

## 2026-09-01 · `45cce4d` · 认顶层的问答两列

**动了**：`src/finevision_to_sharegpt/sample_parser.py`、`tests/test_sample_parser.py`、
`scripts/probe_dataset.py`（顺带修了一处误导输出）

**为什么**：RefCOCO 的列是 `question_id, image, question, answer, ...`，问答就摆在
顶层两列里，而解析器只认 `texts`/`messages`/`conversations` 三个会话列，于是每一行
都判 `missing_text`，整个数据集颗粒无收。新增 `QA_FIELDS`，只认完整的一对。

**对另一侧的影响**：**有，见文首待办。** 解析结果变了——原来判 `rejected` 的行现在
能过。库里已经标成 `rejected` 的不会自动重试。

---

## 2026-09-01 · `e23dd67` · 描述列是数组时不再整集被拒

**动了**：`src/finevision_to_sharegpt/sample_parser.py`、`tests/test_sample_parser.py`

**为什么**：Flickr30k 一张图配五条描述，`caption` 列是字符串数组，而兜底逻辑
`isinstance(value, str)` 直接不成立。列表取第一条非空的，不把五条拼成一段
没人写过的答案。

**对另一侧的影响**：同上，解析结果变了。

---

## 2026-09-01 · `03b8b45` · done 行不再被补扫打回 pending

**动了**：**`src/finevision_to_sharegpt/db/mysql_ledger.py`（你的文件，那时还没有归属约定）**、
`tests/test_db_integration.py`、`README.md`

**为什么**：`db-scan` 把读到的每一行都写成 `pending`。要补上第一遍没存的英文原文
就得清掉 `dataset_cursor` 从头重读，而那样跑在旁边的翻译已完成的行会被一路重置，
十几天的 GPU 结果悄悄作废。upsert 改成
`status = IF(sample_source.status = 'done', 'done', VALUES(status))`。

**对另一侧的影响**：**动了你的核心 SQL。** 语义变化：`done` 是终态，任何 upsert
都不再降级它。要重翻某批必须显式 `UPDATE ... SET status='pending'`。
补了一个真库集成测试覆盖「翻完 → 清 cursor → 补扫 → 状态和译文都还在」。
这个改动早于归属约定，如果你觉得实现方式不合适，改法由你定，
但**并行安全这个性质要保住**——用户的补扫和翻译是同时跑的。

---

## 2026-09-01 · `e86a64c` / `932fede` · 库存盘脚本

**动了**：`scripts/db_inventory.py`（新建）、`tests/test_db_inventory.py`、`README.md`

**为什么**：`db-status` 回答「这次跑到哪了」，开跑前想知道的是另一个问题——
之前几次灌库到底进了什么、原文存没存。一次扫描折出每个数据集的
done/pending/中文/英文/有译文，附译文的模型与提示词版本、各表体积。

**对另一侧的影响**：这个脚本按归属表现在**归你**。两个已知点：
① `information_schema.table_rows` 是抽样估算，真机上偏到 115,012 对 25,243,243，
输出里已标注；② storage 只查 `BASE TABLE`，否则 185 个空视图把四张真表埋了。

---

## 2026-09-01 · `2de573e` · 打开 store_conversations

**动了**：`configs/translate_5m.json`、`configs/translate_zips.json` 的 mysql 段

**为什么**：关着的时候账本只存 id/图片路径/状态，英文原文留在 parquet 里。
省几 GB，但 `db-export` 只导得出中文那部分——英文样本在库里没有源文本，
用户要的「入库后用终端动态配比」就缺了一半。而且这个开关**不能事后补**，
关着跑完的行不会回填。500 万条原文约 5~10 GB，相比 1.3 TB 图片可忽略。

**对另一侧的影响**：**这条就是上次判断反了的那次。** 现在起：
① `sample_source.conversations` 会被写满，库的体积估算要按存原文算；
② 翻译本身会给它碰到的每一行补上英文原文，所以**为这 500 万行单独补扫是多余的**；
③ `db-export` 的那句「英文样本没有源文本会被跳过」的提示，在新配置下不再触发。

---

## 2026-09-01 · `33e9b09` / `12e5eb5` · 规模定为 500 万，每条都翻

**动了**：`configs/sampling_plan_5m.json`、`configs/translate_5m.json`、
`configs/fv_counts.txt`、`scripts/plan_sampling.py`

**为什么**：实测 4 个实例约 4.3 条/秒（生产侧 47 条/秒，瓶颈确认在 GPU）。
913 万要 24 天不现实，500 万 13.4 天可接受。`chinese_ratio` 0.7 → **1.0**：
配上 `store_conversations`，库里每条同时有英文原文和中文译文，
中英比例变成查询时挑哪一列，不用为换比例重翻。
另外把新增的 `the_cauldron`（188 万）、`Flickr30k`、`RefCOCO` 归了类，
16 个 `text_*` 写进排除名单。共 166 个数据集。

**对另一侧的影响**：`lang_assigned` 会**全是 `zh`**，不再有 `en`。
按 `lang_assigned` 区分中英的查询在这一轮数据上失效——
英文要从 `sample_source.conversations` 取，中文从 `sample_translation` 取。
这直接关系到你那边动态配比的 SQL 怎么写。

---

## 2026-09-01 · `9d98323` / `bf73f2b` · 后端池改为本机 4 个 vLLM 实例

**动了**：`configs/backend_config.json`

**为什么**：改用本机 `start_vllm_4x` 起的实例（8001~8004）。实例自报的
`served-model-name` 是 `Qwen3.8-27B`，配置里写的 3.6 对不上，四个后端全部
404。远端那个条目 key 留空由用户自己填。

**对另一侧的影响**：没有。纯后端连接配置，不碰库。

---

## 2026-09-01 · `c8dcb3c` / `70b05e1` / `1c18f84` · 三个压测工具

**动了**：`scripts/bench_backend.py`（新建）、`configs/translate_bench.json`、
`configs/export_bench.json`

**为什么**：工期一直是猜的。冒烟 300 条只灌满 1.2 批，量出来的是一次往返的延迟
不是吞吐；后来发现真实数据只有 3~5 条/秒，而单实例玩具负载能跑 43 条/秒。
用 `export-zips`（同样读盘解析写图，只是不调模型）量出生产侧 47 条/秒，
才确认瓶颈在 GPU 不在 I/O——两种结论对策完全相反。

**对另一侧的影响**：没有。三个都是只读或独立输出目录。

**顺带一提**：`bench_backend.py` 显式禁用了代理。环境里的 `HTTP_PROXY` 会把
发往 127.0.0.1 的请求吞掉，`curl` 就是这么返回空的——你写诊断脚本时留意。
