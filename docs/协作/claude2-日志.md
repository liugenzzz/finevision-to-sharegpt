# Claude 2 · 流水线与数据逻辑 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 任务方向变了（2026-09-02）

用户暂停翻译，改成**先把 mm_general 下能用的数据全部分类并灌进库**。翻译等库
建好再说。分工按用户的说法：Claude 1 管数据库代码和数据筛选，Claude 2 管流水线
逻辑并在他改完之后把整条链路跑通验证。

在动手之前，有几件事必须先对齐，否则会白干。

### 一、「能注册就能进数据库」这句话现在有两种读法，差别很大

用户的前提是「能注册 = 能进库」。按现在的实现，取决于「进库」指什么：

- **读法 A：db-scan 不报错、每行都写进 `sample_source`。**
  这个**已经成立**，而且数据库侧从来没拦过谁。库里那 942 万条 `text_*` 就是
  证据：注册成功、入库成功、状态全是 `rejected`，一条都不能用。
- **读法 B：每一行都是可用样本。**
  这个**做不到**，也不该由数据库代码去解决——纯文本数据集本身没有图片字节，
  解析器怎么改也变不出来。

**如果按 A 去改数据库代码，是白改。** 真正要动的是**注册这一层的门槛**：
`discover_datasets` 现在只检查「目录里有没有 `.parquet`」，这个门槛太低了。
把它升级成「probe 通过才注册」，「能注册 = 能入库 = 能用」三者才真正一致，
用户要的那个性质才成立。

`dataset_registry.py` 和 `register_datasets.py` 归我，这个改动我来做。
**但要先确认用户要的是 B**（我判断是），别我改完门槛、你那边又按 A 改了一遍。

### 二、顺序不能反：必须 probe → 注册 → 灌库

`rejected` 是终态（见下面第三条待办）。先把 mm_general 整个灌进去、指望以后
修好解析器再补，这条路走不通——那些行永远不会被重读。

所以筛选必须发生在灌库**之前**。工具是现成的，别重造：

```bash
python scripts/probe_dataset.py <root> --all
```

它跑的就是入库时用的那个 `parse_row`，判断完全一致。四种结论：
✅ 可注册 / ⚠️ 缺图 / ⚠️ 缺文本 / ❌ 读不了，附列名和一条解析出来的示例。

用户要「不能注册的告诉他原因」——probe 的输出直接就是原因，不用另写。

### 三、全量灌库的体量要先算，别开跑了才发现

几个数需要在开跑前有答案：

- mm_general 下能读的总行数（FineVision 1581 万 + OmniScience / SA1B 待定）
- 库里已有的 942 万 `text_*` 是删掉还是留着——留着的话每次全量扫描都白读一遍
- `store_conversations` 现在是 `true`，英文原文会入库，表会比上次大得多
- **`db-scan` 默认会写图片文件**。全量灌库如果不加 `--no-images`，落盘是几 TB。
  路径是内容哈希，加了 `--no-images` 也完全正确，真正消费时自然会写

### 四、我刚改过 db-scan 会用到的一个语义

`limit_reached` 现在把 `skipped` 也算进配额（见下面那条 `limit` 的记录）。
全量灌库 `limit_per_dataset` 是 `None`，不受影响；但如果你给 `db-scan` 配了
limit，续跑行为跟以前不一样了，是「总量」不是「本轮增量」。

### 五、验收怎么做

用户要求「他改完之后整条链路跑通、不要有 bug」。我这边能做的是：
全套单元测试 + 真库集成测试（需要 MySQL 环境先装好）+ 一个小规模端到端
（几个数据集走完 db-init → db-scan → db-status → db-export）。

**前提是 MySQL 得先装起来**——服务器换了机器，`/mnt/fv` 连同 datadir 一起没了。
用户想让 MySQL 和 Python 用同一个 conda 环境，而脚本现在做不到（见下面第一条
待办的更正）。这个卡在你那边，装好告诉我，我来跑验证。

---

## 待 Claude 1 处理

### 一、`setup_local_mysql.sh` 的默认 BASE 落在不持久的盘上

服务器重启了一次，`/mnt/fv` 整个没了——conda 环境和 MySQL 的 datadir 一起丢。
而代码、模型、数据都在 `/mnt/si003010kcx0/mmdata/` 下，安然无恙。这块盘持久，
`/mnt/fv` 不持久。

用户是被这个坑到的，不是配置写错。两点归你判断：

1. 默认 `BASE` 是否该改掉，或者至少在脚本开头对「BASE 所在卷是否持久」
   给一句提示——丢的是 2524 万行账本，不是几个临时文件。
2. 二进制和数据是否该分开。用户想让 `mysqld` / `mysql` 跟 Python 装在同一个
   conda 环境里（`conda activate` 之后命令行直接可用，他遇到的
   `mysql: command not found` 就是这么来的），而 datadir 单独放。

   **但 `FV_MYSQL_ENV` 现在做不到这件事**：脚本里是
   `conda create -y -p "${ENV_PREFIX}" ...`，而 `conda create` 对着一个已存在
   的 prefix 会直接报 `prefix already exists` 退出。指向已有的 fv 环境等于
   让脚本挂掉。

   我给用户的绕法是先手动 `conda install -p <fv环境> mysql-server mysql-client`，
   装完脚本那个 `[ ! -x .../bin/mysqld ]` 的判断就会跳过创建、直接用这些二进制。
   能用，但要用户先跑一条额外命令才成立。真要支持这个用法，脚本里判断一下
   prefix 存不存在、存在就走 `conda install -p`，会自然得多。这是你的文件，
   改不改由你定。

我没有动这个脚本，按归属它是你的。

### 二、MySQLLedger.is_consumed 不看 completed_ids，灾难恢复时 resume 等于失效

`_prepare_zip_run` 在 `resume: true` 时从 `output/*/train.jsonl` 读出
`completed_ids` 传给 `open_ledger`，`JsonlLedger.is_consumed` 用它跳过已完成的
样本。但 `MySQLLedger.is_consumed` 只查 `plan.consumed_ids`（来自数据库），
从不读这个集合——`completed_ids` 在 MySQL 账本里只被 `mark_done` 加，从不被读。

平时没问题，数据库本来就该是权威。**但账本丢了之后就要命**：服务器重启把
`/mnt/fv` 连同 datadir 一起抹了，而 `output/run5m/train.jsonl` 好端端地在持久盘
上。此时带着空库重跑，`resume: true` 一条都不跳，几万条已翻的会重做一遍，
而且往同一个 jsonl 里追加重复记录。

我给用户的临时方案是这轮改用文件账本（去掉 `mysql` 段），JSONL 当断点，
不动代码。要不要让 MySQL 账本也认这个集合，归你判断——
`is_consumed` 里加一句 `sample_id in self.completed_ids or ...` 就够，
代价是启动时多读一遍 jsonl（本来就读了）。

另外这轮结束后需要一个「从 JSONL 灌回账本」的路径：`train.jsonl` 是中文、
各数据集的 `raw.jsonl` 是英文，`sample_id` 形如 `数据集:分片名:行号`，
`sample_source` 和 `sample_translation` 需要的字段都能还原。这是离线活，
不该为它停 GPU，但迟早要有。

---

### 三、`rejected` 是终态，解析器修好之后不会自动重试

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

## 2026-09-02 · `limit` 改为总量语义，续跑不再超配额

**动了**：`src/finevision_to_sharegpt/zip_pipeline.py`（**共有文件，按约定声明**）、
`tests/test_zip_pipeline.py`、`tests/test_directory_datasets.py`

**为什么**：三处 `limit_reached` 都只数 `written`/`chinese`，不数 `skipped`。
续跑时上一轮已完成的行走 `is_consumed` 变成 `skipped`，不计入配额，于是**每重启
一次就在已有产出之上再翻一整份**。用户的 500 万跑了 23.7 小时、36.7 万条之后
机器被换掉，正要续跑——不改的话已经动过的四个数据集会各自翻出双份，而且每次
中断都再叠一层，十几天里必然会中断好几次。

改成 `written + chinese + skipped >= limit`，`limit` 的含义从「这一轮新翻多少条」
变成「这个数据集最终贡献多少条」。这也更贴近配置读起来的样子：
`plan_sampling` 写出的 `limit` 本来就是配额，不是增量。

**对另一侧的影响**：**有两点。**

① 我之前跟用户说过「`limit` 是这一轮新翻多少条，扩产要写增量」，那个说法现在
作废了——扩产直接写新的总数。如果你的文档里有同样的说法，一起改掉。

② **MySQL 模式下有个我没解决的边角**：`scan_plan` 的 `consumed_ids` 把
`done` 和 `rejected` 都算进来，所以 `rejected` 的行在续跑时也会变成 `skipped`
并计入配额。拒绝率高的数据集续跑后会**填不满**配额（首轮里 `rejected` 是不计
数的，前后不一致）。文件账本没这个问题——`JsonlLedger` 只在 `mark_done` 时
记 id。要彻底解决得让流水线能区分「跳过是因为已完成」还是「跳过是因为已拒绝」，
那要动 `ConsumptionLedger` 的接口，归你。用户现在跑的是文件账本，不受影响。

**验证**：224 passed / 1 skipped。三个新测试撤掉修复就会红（验证过）。
`test_directory_dataset_resumes_without_repeating` 写的是旧语义，改成「同样的
limit 再跑写 0 条、把 limit 调大才继续」，它原本要证明的「续跑不重复」保住了。

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
