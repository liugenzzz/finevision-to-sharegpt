# Claude 1 · 数据库与入库 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 2026-09-04 · 定位翻译慢的真凶：不是数据库，是 _fallback_translate 按小时占住线程

用户反馈翻译慢（1.82 → 2.92 条/秒），怀疑数据库拖后腿。**全量实测过了，不是。**
真因在你那边，下面是证据和三个方向。

### 先排除数据库（实测，不是估计）

真 MySQL 8.4.2 上压 `claim + mark_done + record_translation` 三次写：

```
线程  1  批 200  池  8   =  225.7 行/秒
线程 32  批 200  池  4   =  573.9 行/秒
线程 32  批 200  池  8   =  646.9 行/秒
线程 32  批 200  池 16   =  790.9 行/秒   ← 拐点
线程 32  批 500  池 32   =  740.6 行/秒   ← 不再涨
线程 64  批1000  池 32   =  713.5 行/秒   ← 批调大反而略降
```

算账：流水线 1.82 条/秒 = 每行 549 ms；数据库单线程每行 4.4 ms。**占 0.8%。**
把数据库变成瞬间完成，1.82 只会变成 1.835。

所以这几件我明确**不做**，别再往这个方向想：加写库线程、加灌库进程、调大
`batch_size`（实测越大越慢）、调大 `pool_size`（16 就到顶，而且翻译路径根本用不满）。

### 真凶：回退路径没有总时长上限

`latency_ms` 量的是纯模型调用（`started` 在 handler 里、请求前一行设），不含排队。
但用户真实数据里最慢一条是 **11,790,886 ms = 3.27 小时**，而 `request_timeout` 是 300 秒。

对不上，因为它量的是整个 `translate_sample`：

```python
try:
    response = client.chat(整段对话一次翻完)     # 1 次
    conversations = _parse_conversations(response, sample)
except Exception:
    conversations = _fallback_translate(...)     # 退化成逐句串行
```

`_fallback_translate` 是 `for turn in sample.turns` 串行调用，**每句各自吃满 300 秒**，
句数不限。`11,790,886 ÷ 300,000 ≈ 39` —— 一条 39 轮的对话走回退，就占住一条线程 3.27 小时。

### 损失有多大

用户现场数据（5 个后端全活着，最近窗口产出 150–213，**负载均衡本身没问题**）：

```
后端         累计     最近5分钟  条/秒   平均ms     最慢ms
3.8_vl     506849      213    0.71   17946   2591401
vllm-8002  256090      150    0.50   38177  11790886
vllm-8001  255843      176    0.59   38310  11822644
vllm-8003  255822      161    0.54   38202  11812938
vllm-8004   73137      176    0.59   71535  11595746
合计 2.92 条/秒
```

拿 vllm-8001 算：`有效并发 = 0.59 × 38.3 ≈ 22.6`，而配置里 `concurrency` 是 64。
**只有约三分之一的槽在转，其余全卡在多小时的回退请求里。**
平均 38 秒这个数本身也说明回退触发率很高——正常一次调用不该 38 秒。

### 三个方向（都归你）

1. **给回退加总预算**（根因）。现在是每句 300 秒、句数不限。应改成按整条样本设上限：
   整条最多 N 秒，或最多回退 M 句，超了判失败让它进 `failed`，别占着线程。
2. **查回退为什么触发这么频繁。** 回退只在 `_parse_conversations` 失败时走。
   可能是 `<think>` 又漏了，或者长对话把 `max_tokens` 撑爆导致 JSON 截断。
   现在解析失败的原因没记下来，加个按原因计数就能看出来。
3. **`request_timeout: 300` 太宽松**，降到 60 左右。翻一句不该要 5 分钟。

### 我改了什么

`scripts/check_backend_throughput.py`（新增）：连着库查各后端的累计/最近产出、
平均与最慢延迟，并直接算有效并发、把 `最慢ms ÷ request_timeout` 换算成回退句数。
**只读**，显式传 `ensure_schema=False`——默认构造会跑 DDL 补列，对一张几千万行的
表且正在跑任务时是灾难。

第一版把 `backend_name IS NULL` 的行判成「掉线的后端」，是误报：那 367,626 条是
`db-restore` 灌回来的历史产出（restore 调 `record_translation` 时 `backend_name`
传 `None`）。已修，现在单列说明。

顺带一条状态：**`db-restore` 已经跑过了**，库里 `done` 1,716,837 行，`failed` 26。

---

## 2026-09-02 · 日报：灌库前的准备做完了，卡在灌库本身，翻译入库挂起

给 Claude 2 的当日汇总。前面四条分条日志是细节，这条是**你接手前必须知道的状态**。

### 当前状态（用户已挂起，不要自行推进）

用户把「翻译产出入库」这件事挂起来了。库现在是**空的但表已建好**，进度停在这里：

| 步骤 | 状态 |
| --- | --- |
| MySQL 装进 fv 环境 | ✅ 完成 |
| `db-init` 建表建视图 | ✅ 完成（**但要重跑一次，见下**） |
| `probe_dataset --all` 扫 mm_general | ⏸ 跑了一半，用户中断 |
| `db-scan` 全量灌库 | ❌ 未开始 |
| `db-restore` 回填 36.7 万 | ❌ 未开始 |
| 恢复翻译 | ❌ 未开始，等前两步 |

### 三件你必须知道的事

**1. `db-init` 要重跑一次，否则新列和视图都不对。**

我今天给 `sample_source` 加了 `source_lang`（详见上一条日志）。用户的表是加列之前
建的，`ensure_schema` 会查 `information_schema` 就地补列；而 `v_sample_source_*`
是 `SELECT *`，MySQL 建视图时把列固化了，老视图看不见新列，得 `CREATE OR REPLACE`。
`db-init` 两件事都做，跑一次即可，幂等。

**2. 灌库顺序不能反：先 `db-scan`，再 `db-restore`。**

实测（10 行 parquet，其中 1 行当年被拒、JSONL 里没有）：

```
restore→scan   丢库前 10 行  ->  恢复后  9 行   ← 永久漏 1 行
scan→restore   丢库前 10 行  ->  恢复后 10 行
```

`db-restore` 把水位线推到「已产出记录里最大行号」，而 `db-scan` 走 `for_ingest`
语义，从水位线**往后**读。夹在水位线下方、JSONL 里又没有的行（当年被拒的）从此
扫不到，`db-retry-rejected` 也救不了——它只能重置库里已有的 `rejected` 行。

**这条还没有回归测试锁住**，是我临时脚本验出来的。我本来要补进
`test_db_integration.py`，被挂起打断了，**这是我这边最优先的欠账**。

**3. `open_dataset` 多了第四个参数 `source_lang`（默认 `"en"`）。**

我改了 `zip_pipeline.py:297` 的调用点传 `dataset.source_lang`。你要是有别的调用点，
不传不报错，但落库永远是 `en`。`lang_assigned` 的语义和取值一点没动，你的翻译分流
逻辑不受影响。

### 归你判断的两件

**a. 中文原生集要不要跳过翻译。** 库里现在能认出它们了（`source_lang='zh'`），
但「认出来之后分流时跳过」是分流的事，归你。目前它们仍会按 `chinese_ratio` 走。

**b. `probe_dataset` 会去读点开头的目录。** 用户跑
`probe_dataset.py <mm_general> --all --only-bad` 时，第一条输出是：

```
.cache   ❌读不了   没有 parquet 也没有 zip，里面是 .metadata×5, .lock×5
```

`.cache` 是 HuggingFace 下载留下的，不是数据集。`--all` 用的是
`sorted(root.iterdir())`，没排除点目录，所以每个这种目录都会被当成一个集合报一遍，
还会去 rglob 它。用户看着像卡住了（实际是在遍历），中断了。
**这是你的文件的判定入口，我没动**——要不要跳过 `.` 开头的目录你定。

### 我今天改了什么（四个提交）

```
99704d9  feat: sample_source 加 source_lang，与 lang_assigned 分开
9b11e3d  feat: probe_dataset 加 --only-bad/--json，汇总按结论分组   ← 动了你的文件
25ff682  docs: 部署文档改为把 MySQL 装进已有 fv 环境，并挑明可写≠持久
af622df  feat: db-retry-rejected 让改好的解析器重看被拒的行
```

`9b11e3d` 动了你的 `scripts/probe_dataset.py`：只加开关没动判定逻辑
（`dataset_probe.py` 一行没碰），但汇总行格式变了，撞掉了你
`test_all_probes_every_subdirectory_and_survives_a_broken_one` 的断言，
我改成了 `"共 2 个，可注册 1 个"`。觉得不合适直接改，我不锁这块。

测试：258 passed（真 MySQL 8.4.2）／233 passed + 25 skipped（无库），
`ruff --select F,E9` 干净。

### 我的欠账清单

1. **给「先 scan 再 restore」补回归测试**（最优先，上面第 2 条）。
2. `MySQLLedger.is_consumed` 仍不读 `completed_ids`——`db-restore` 顶掉了这次的急，
   但这个口子还开着。
3. gap 内的 `rejected` 行仍计入配额，起点之下的不计。要彻底修得改 `is_consumed`
   的契约。`limit_per_dataset` 是 `None` 的全量灌库不受影响，不急。

---

## 2026-09-02 · sample_source 加 source_lang，区分「源文本是哪国话」和「被分到哪一侧」

**动了**：`db/schema.py`、`db/ledger.py`、`db/mysql_ledger.py`、`db_commands.py`、
`dataset_registry.py`、`cli.py`，加两条集成用例。**改到了 `zip_pipeline.py` 一行**
（`open_dataset` 调用点），见下。

**为什么现在做**：`lang_assigned ENUM('zh','en')` 的语义是「这条被分到中文侧还是
原文侧」，不是「文本实际是什么语言」。中文原生的集合设 `chinese_ratio: 0` 之后会被
标成 `'en'`——存的是中文，标签是英文，`db-export --lang zh` 直接漏掉它们。

用户要把中文原生的集合并进来，所以得分开记。**赶在全量灌库之前做**：
`sample_source` 现在是空的，加列不要钱；等 2500 万行进去再 ALTER 就是另一回事。

**加了什么**：

- `sample_source.source_lang VARCHAR(16) NOT NULL DEFAULT 'en'`。没用 ENUM，
  是为了以后加 `ja`/`multi` 不用改表。每行多约 4 字节，2500 万行约 100 MB。
- 注册表里按数据集写：`{"zh_native": {"dir": "...", "source_lang": "zh"}}`，
  不写就是 `en`。值经 `RegisteredDataset` → `DatasetVersion` → `claim` 落库。
- `db-export --source-lang zh`，和 `--lang` 是**两个独立过滤条件**，别混。

**就地补列**：`ensure_schema` 现在会查 `information_schema` 再决定要不要 ALTER。
MySQL 没有 `ADD COLUMN IF NOT EXISTS`（那是 MariaDB 的），所以不能照抄。
用户的库已经 `db-init` 过了，**重跑一次 `db-init` 就会补上这一列**。

**对另一侧的影响**：

1. **`open_dataset` 多了第四个参数 `source_lang`（有默认值 `"en"`）。**
   我把 `zip_pipeline.py:297` 的调用点改成传 `dataset.source_lang` 了。
   你要是有别的调用点，不传也不会报错，但那样落库的就永远是 `en`。
2. **视图要重建。** `v_sample_source_*` 是 `SELECT *`，MySQL 在建视图时就把列固化
   了，老视图看不到新列。`db-init` 走的是 `CREATE OR REPLACE VIEW`，重跑即可。
3. `lang_assigned` 的语义和取值**一点没动**，你的翻译分流逻辑不受影响。
   中文原生集要不要跳过翻译，那是分流的事，归你——我只负责它在库里能被认出来。

---

## 2026-09-02 · probe_dataset 加 --only-bad/--json；实测出「恢复顺序反了会静默漏行」

**动了**：`scripts/probe_dataset.py`（两个开关 + 按结论分组的汇总）、
`tests/test_probe_dataset.py`（改一条断言 + 加两条）。**注意我改到了你的文件**，
见下面第 3 条。

**1. 恢复顺序：必须先 `db-scan` 再 `db-restore`，反了会永久漏行。**

实测（10 行 parquet，其中 1 行当年被拒、JSONL 里没有）：

```
restore→scan   丢库前 10 行  ->  恢复后  9 行   ← 漏 1 行
scan→restore   丢库前 10 行  ->  恢复后 10 行
```

原因：`db-restore` 把水位线推到「已产出记录里最大的行号」，而 `db-scan` 走
`for_ingest` 语义 —— `ScanPlan(start_row=watermark)`，水位线**下面**的行一行都不读。
夹在水位线下方、JSONL 里又没有的行（当年被拒的）就此扫不到，
`db-retry-rejected` 也救不了（它只能重置库里已有的 rejected 行）。

反过来先 scan 安全，靠两个既有保护：`dataset_cursor` 的 upsert 是 `GREATEST`
（水位线只进不退），`sample_source` 的 upsert 是
`status = IF(status='done','done',VALUES(status))`（done 行不降级）。

**这条还没有回归测试锁住**，是临时脚本验出来的。我下一步补进
`test_db_integration.py`。在那之前谁都别把顺序写反。

**2. 用户屏幕上那个 `{"rows": [], "totals": {}}` 不是故障** —— `db-init` 只建表，
没灌数据，空统计是对的。他还试了裸敲 `db-init`（`command not found`），
说明命令前缀容易漏，文档里的命令以后都写全 `python -m finevision_to_sharegpt ...`。

**3. 对另一侧的影响 —— 我改了 `probe_dataset.py`，这是你的文件。**

只加东西没动判定逻辑（`dataset_probe.py` 一行没碰）：

- `--only-bad`：只列入不了库的。185 个集合全打出来没法看。
- `--json FILE`：机器可读，字段有 `dataset/verdict/label/advice/detail/columns/rows_*`。
- 汇总行从 `可注册 1 / 2: good` 改成「共 N 个，可注册 M 个」+ 按结论分组列名字。
  **这行格式变了，撞掉了你 `test_all_probes_every_subdirectory_and_survives_a_broken_one`
  的断言，我改成了 `"共 2 个，可注册 1 个"`。** 测试意图没动。

如果你觉得这几个开关该长在别处、或者汇总格式你有别的打算，直接改，我不锁这块。

---

## 2026-09-02 · 重写部署文档第 9 步：MySQL 装进 fv 环境，datadir 别再放临时卷

**动了**：`docs/服务器部署说明.md`（开头加持久性警告，第 9 步 MySQL 整节重写）。
代码未改。

**为什么**：用户换机后要重装，问「mysql 咋装、咋设置」。翻文档发现这一节写的还是
丢账本之前的做法，两处都正好是坑过他的地方：

1. datadir 示例写死 `/mnt/fv/mysql` —— 就是重启即清空、丢掉 2524 万行的那个卷。
2. 教的是 `conda create -p <BASE>/env` **另建**一个环境。用户明确说过「感觉太别扭
   了」，而 `setup_local_mysql.sh` 早就支持 `FV_MYSQL_ENV` 装进已有环境，文档没跟上。

**改成什么**：

- BASE 选址单独一小节，判据是「上次重启后还活着的东西在哪个卷」，让他用
  `df -P .` 从代码目录问出来，**不给可抄的路径**（CLAUDE.md 那条教训）。
- 方案一改成装进已有 fv 环境，用 `FV_MYSQL_ENV="$CONDA_PREFIX"`，同样不手写路径。
- 新增「每次重启之后要重跑」——mysqld 不开机自启，这是重启后连不上的头号原因。
- 新增「不用去调 max_connections」：`SET GLOBAL` 每次重跑脚本都会被 my.cnf 覆盖，
  而且连接数看 `pool_size` 不看并发。用户之前手工设过 500，得说清楚那是白设。
- `on_connect_error` 补上 `fallback` 的危害说明，推荐 `fail`。
- 补 `db-init` / `db-restore` 两节收尾，之前文档到「配置」就断了，没说建完表干嘛。

**对另一侧的影响**：

- 文档现在推荐 `on_connect_error: "fail"`（原来示例是 `fallback`）。你那边的示例
  配置如果还是 `fallback`，要么改，要么在日志里说明为什么保留，别两份文档打架。
- 部署路径不再出现任何具体挂载点，你以后写文档也照这个来：给命令让用户问出来，
  别给可抄的字符串。

---

## 2026-09-02 · `db-retry-rejected`：解析器修好之后，把被拒的行放回去重扫

**动了**：`db/mysql_ledger.py`（`rejected_breakdown` / `retry_rejected` /
`_rejected_filter`）、`db_commands.py`（`run_db_retry_rejected`）、`cli.py`
（新命令 `db-retry-rejected`），外加三个集成用例。

**为什么**：你在 `45cce4d` 补了 RefCOCO 的顶层问答列、`e23dd67` 补了数组型
描述。解析器变了，可之前被这两个 bug 判成 `rejected` 的行**永远不会再被读到**
——`scan_plan` 只回退到最早的**未完成**行，而 `rejected` 是终态。
`Flickr30k` 就在用户那批图片目录里，属于受影响的一批。
在此之前想救只能手写 SQL，容易连不该动的一起动了。

**机制上不用碰 `dataset_cursor`**：`_UNFINISHED_PREDICATE` 认 `pending`，而
`start = watermark if unfinished is None else min(unfinished, watermark)`。
把 `rejected` 改回 `pending`，扫描起点自己就退回去了。
（对照 `db-scan` 想重扫必须先 `DELETE FROM dataset_cursor` —— 那是因为它走
`for_ingest=True` 的水位线语义，两条路径别记混。）

**故意难用的地方**：不给 `--dataset` / `--reason` / `--all` 直接报错，且默认
只预览、要 `--apply` 才写。原因就是你日志里那条警告——库里那 942 万条 `text_*`
的拒绝是**对的**，纯文本无图，放回去只会让之后每一轮都白读一遍。
命令的报错文案里也把这句话写进去了。

典型用法（先看，再动）：

```bash
python -m finevision_to_sharegpt db-retry-rejected --config <cfg>            # 列出各原因有多少行
python -m finevision_to_sharegpt db-retry-rejected --config <cfg> --dataset Flickr30k
python -m finevision_to_sharegpt db-retry-rejected --config <cfg> --dataset Flickr30k --apply
```

**对另一侧的影响**：

- 你以后再修解析器，不用再来找我写 SQL，也不用改 `dataset_cursor`，直接按
  `--reason` 圈一批重置就行；`reject_reason` 现在是有用的，落库时请继续写准。
- 重扫会重新读 parquet 也重新翻译，**配额是照常扣的**。翻译暂停期间跑
  `--apply` 是安全的（只改状态不产出），但等重新开跑之前先想清楚 `limit`。
- 状态机没有新增取值，`retry_rejected` 只做 `rejected -> pending`，
  `done` 的行一律不碰。

---

## 2026-09-02 · 记录换机后的新路径，以及一个会让训练找不到图的前缀陷阱

**动了**：只动这份日志，代码未改。

**为什么**：用户告知换机之后产出还在，但位置变了。这两个路径此前只有 Claude 2
知道，记在这里让两边都拿得到：

```
图片   /mnt/si003010kcx0/mmdata/mm_images/fv_images
数据   /mnt/si003010kcx0/mmdata/mm_dataprocess
```

（`/mnt/fv` 不持久，重启即清空，账本就是这么丢的；上面这两个在持久卷上。）

**对另一侧的影响**：**有一个坑，改配置之前务必先看。**

ShareGPT 记录里的图片是相对路径，而**前缀取自 `images_root` 的目录名**——
`_image_store_from_images_root` 是 `ImageStore(output_root=images_root.parent,
images_dir=images_root.name)`，落到记录里就是 `<目录名>/<数据集>/<hash>.<ext>`。

`translate_5m.json` 没有显式 `images_root`，推导成 `output/run5m/images`，
所以已产出的 36.7 万条记的都是 `images/<数据集>/<hash>.jpg`。

**如果把 `images_root` 直接改成 `.../mm_images/fv_images`，目录名变成
`fv_images`，续跑写出的新记录就是 `fv_images/<数据集>/<hash>.jpg`。**
同一个数据集里一半旧前缀一半新前缀，训练时必然有一半找不到图，而且不会报错，
只会静默少掉一半样本。

正确做法取决于图片在盘上的实际层级，我没有猜：

- 若实际是 `.../fv_images/<数据集>/<hash>.jpg`
  → 想保持 `images/` 前缀，需要让 `images_root` 的**目录名**仍是 `images`，
    也就是把图片放在 `.../fv_images/images/<数据集>/...` 之下，
    或者接受前缀变更并把旧记录一起改写。
- 若实际是 `.../fv_images/images/<数据集>/<hash>.jpg`
  → `images_root` 设成 `.../mm_images/fv_images/images`，前缀不变，无需改动。

已让用户确认盘上的真实层级。**在确认之前不要改任何配置的 `images_root`。**

`db-restore` 本身不受影响——它把 JSONL 里的相对路径原样读回，记的是什么就存
什么。风险只在新旧记录前缀不一致。

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
