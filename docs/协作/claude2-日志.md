# Claude 2 · 流水线与数据逻辑 · 变更日志

新的在上。格式见 [README](README.md)。

---

## 2026-09-02 · 注册门槛提到「解析器读得动」，注册 = 能入库 = 能用

**动了**：`src/finevision_to_sharegpt/dataset_probe.py`（新建）、
`dataset_registry.py`、`scripts/probe_dataset.py`、`scripts/register_datasets.py`、
`tests/test_dataset_probe.py`（新建）、`README.md`

**为什么**：用户要「能注册就能进数据库」。原来 `discover_datasets` 只检查
「目录里有没有 `.parquet`」，纯文本数据集轻松通过，然后在账本里填满 `rejected`
行——每次扫描白读一遍，而且 `rejected` 是终态，之后修好解析器也不会重试。
库里那 942 万条 `text_*` 就是这么来的。

现在注册前逐个打开数据、用入库时那个 `parse_row` 跑几行，只有 ✅ 才写进
registry，被拒的连原因带列名一起打印。

**关键的实现选择**：判断逻辑从 `scripts/probe_dataset.py` 挪进了包里的
`dataset_probe.py`，脚本变成薄壳。**注册和入库必须共用同一份判断代码**——
两份实现迟早会漂移，而「一致」正是这次改动的全部意义。

**性能上的取舍**：`auto_discover` **默认不验证**。它在每条命令启动时都跑，
191 个数据集各开一个分片太贵。验证是 `register_datasets.py` 的默认行为
（付一次代价、把结果固化下来），`auto_discover` 要的话显式写
`{"verify": true}`，`--no-verify` 可以关掉脚本的验证。

**对另一侧的影响**：**有，而且正是你要的那件事。**

① **你不需要改数据库代码来实现「能注册就能进库」了。** 数据库侧本来就没拦过
谁——`db-scan` 对任何一行都会写进 `sample_source`，只是解析不了的标 `rejected`。
门槛在注册这一层，现在提上来了。

② **用户要的「数据筛选、不能入库的给出原因」已经有了**，不用另写：
`python scripts/register_datasets.py <root> --dry-run` 直接列出能注册的和
不能注册的（含原因、列名、怎么处理）。想单看某个集合用
`scripts/probe_dataset.py`。两者走的是同一份 `dataset_probe`。

③ 如果你要在数据库侧复用这个判断，**从 `finevision_to_sharegpt.dataset_probe`
导入 `probe_dataset` / `verify_datasets`**，别照抄一份到 `db/` 下面。

**验证**：231 passed / 1 skipped，`ruff check --select F,E9` 干净。
新增 7 个测试覆盖：纯文本集被剔除、坏 parquet 不抛异常只记原因、
每个被拒的都带 reason、verify 与 include/exclude 组合、zip 里的 parquet 也验。
另外拿三个人造数据集（可用 / 缺图 / 缺文本）跑了一遍 `register_datasets.py --dry-run`，
输出确认只写入可用的那个。

**待办**：`configs/datasets.json` 现在还是 `auto_discover: true`（不验证）。
等 MySQL 装好、mm_general 那边 probe 完，应该改成固化的显式注册表——
那才是「注册即可用」真正落到配置里的形态。

---

## 2026-09-02 · probe 跳过点目录，并且不再走完整棵树

**动了**：`src/finevision_to_sharegpt/dataset_probe.py`、`scripts/probe_dataset.py`、
`scripts/survey_roots.py`、`tests/test_probe_dataset.py`

**为什么**：你日报里提的两点，加上一个我顺着查出来的根因。

① `.cache` 被当成数据集报了一遍。点开头的目录是工具留下的（`.cache`、`.git`），
永远不是数据集。`--all` 和 `survey_roots` 现在都跳过，跳了几个会在 stderr 说一句。

② **「看着像卡住」不是错觉，也不只是 `.cache` 的锅。** `probe_dataset` 原来是
`sorted(directory.rglob("*.parquet"))`——**把整棵树的匹配全列出来、排序，然后只取
第一个**。在 4 TB 的网络盘上，为了读五行数据先做一遍全树 stat，慢到看不出在动。
改成 `os.walk` 按序自上而下，找到第一个就停。没有 parquet 时的扩展名普查同样
封了顶（500 个文件足够说清目录里是什么，`.jpg×2000000` 不比 `.jpg×500` 多说明
任何事）。

③ 进度打到 **stderr**，一集一行。这样 `> report.txt` 重定向出来的报告是干净的，
而慢的集合看起来是慢，不是死。

**对另一侧的影响**：没有。判定逻辑（`probe_parquet` / `probe_zip` / 结论取值）
一个字没动，`verify_datasets` 和注册门槛的行为完全不变——只是找那个代表分片的
路径变快了、点目录不再进入候选。你加的 `--only-bad` / `--json` / 分组汇总都没碰。

**验证**：236 passed / 1 skipped，`ruff --select F,E9` 干净。新增 3 个测试：
点目录不出现在报告里、进度只走 stderr、无 parquet 的目录不列举全部文件。
另外造了个含 `.cache` 的目录树跑了一遍，确认输出如预期。

---

## 2026-09-02 · 流水线侧端到端验证报告（无 MySQL）

**动了**：没有改代码，这条是验证记录。

**做了什么**：我这个沙箱起不了 MySQL（没有 conda、docker daemon 没起、apt 无源），
真库那部分我验不了。能验的全验了：造了 5 个人造数据集（3 个可用、1 个纯文本无图、
1 个有图无文本），起了个假 OpenAI 端点，把流水线侧从注册到合并跑了一遍。

| 步骤 | 结果 |
| --- | --- |
| ① `register_datasets` 验证门槛 | 3 个注册，2 个连原因带列名列出 ✅ |
| ② `export-zips` 每集 4 条 | 写 12 条 ✅ |
| ③ 同配额再跑 | **写 0、跳过 12** ✅（修复前这里会再写 12） |
| ④ 配额 4→6 | 补 6 条，共 18 条，**id 无重复** ✅ |
| ⑤ `translate-zips` 每集 3 条 | **9/9 成功、0 失败** ✅ |
| ⑥ 翻译同配额续跑 | 写 0、跳过 9 ✅ |
| ⑦ 配额 3→5 | 补 6 条，共 15 条，无重复 ✅ |
| ⑧ 图片落盘 / `raw.jsonl` | 15 个文件、英文原文在 ✅ |
| ⑨ `validate` | 15 全过、0 拒绝 ✅ |
| ⑩ `merge` 去重 | 30 进 15 出 ✅ |

假后端故意在返回里包了 `<think>` 和 ```json 代码围栏，译文里都剥干净了；
`<image>` 在第一条 human 开头；图片路径是 `images/<数据集>/<sha256>.jpg`。
另外 236 passed / 25 skipped，`ruff --select F,E9` 干净。

**核对了我俩那两个「续跑超配额」的修复会不会打架**（我 `f5a1df8`、你 `820858c`）：
**不会，两段不重叠**。你数的是 `start_row` 之下、根本没被读到的 `done` 行
（`skipped_before`），我数的是 `[start_row, gap_end)` 里读到并跳过的行。
有 gap 的分支里你传的是 `_count_done_before(..., start)` 而不是 `..., watermark)`，
正好避开重叠——上面 ③⑥ 两行就是这个组合的实测。

**我验不了、留给你的**：
`db-init` / `db-scan` / `db-restore` / `db-retry-rejected` / `db-export`，
以及你日报里那条「先 scan 再 restore」的顺序。**那条仍然是最优先的欠账**——
一个没有测试保护的顺序约束，在几千万行上出错是永久性的。

**顺带一个环境敏感点**（不是 bug，暂不改）：`qwen_client` 用的是
`httpx.Client()`，默认 `trust_env=True`，也就是**会读 `HTTP_PROXY`**。
机器上设了代理而 `NO_PROXY` 没覆盖 `127.0.0.1` 的话，所有发往本机 vLLM 的请求
都会被代理吞掉——用户之前 `curl` 本机端点返回空就是这个形状。没改是因为远端
后端可能确实需要走代理。`check_backends.py` 或许该把这点显式报出来。

---

## 2026-09-04 · 完成判据改成「进队列的都被尝试过」，堵住剩下的静默退出

**动了**：`src/finevision_to_sharegpt/backend_pool.py`、`tests/test_backend_pool.py`

**为什么**：上一条我只堵了「后端被摘光」这一个口子，用户一句话点出还有别的。
确实有：`client_factory` 构造就抛（api_base 写错之类）、worker 被任何别的异常
打死——这两种情况下**没有任何后端被"摘掉"**（它们连一次调用都没发出去），
worker 的 `finally` 照常放下哨兵，`finished_workers` 照常凑满，生成器照常返回，
**一条都没产出**。只看 `disabled` 是发现不了的。

改成数数：`produced` 由生产者累加、`consumed` 由 worker 取到真任务时累加。
收尾时 `produced > consumed`（或生产者还卡在 `put` 上）就说明队列没被取空，
抛错并说明**至少多少条从未被尝试过**。这个不变式同时覆盖三种情况——
后端摘光、worker 构造失败、worker 崩溃——错误信息按现场分别措辞。

worker 的异常也不再吞掉了，收集起来在报错里带上第一条。

**对另一侧的影响**：没有，纯失败可见性。但同上一条：**按退出码 0 判断"这轮跑完了"
的脚本，以前的判断本来就是错的**，现在会如实报错。

**验证**：247 passed / 25 skipped，ruff 干净。新增 3 个测试：构造客户端就抛时
报 `worker(s) died`、正常跑完 200 条不报错、报错信息里带上未尝试的条数。

---

## 2026-09-04 · 后端被摘光不再静悄悄地"跑完"

**动了**：`src/finevision_to_sharegpt/backend_pool.py`、`tests/test_backend_pool.py`

**为什么**：用户两轮都遇到「任务在 31/166 就打印统计退出」，看起来像跑完了。
真相是 `disable_backend_after_failures: 20` 把后端挨个摘光——工作线程全部返回，
`finished_workers` 凑满，生成器正常结束，队列里剩下的任务无人认领。
调用方拿到一份局部结果，**打印出来和跑完一模一样**。

十几天的任务里这是最坏的失败方式：不是崩，是看起来成功了。第一轮
`failed=1016`、第二轮 `failed=345`，两次都是这么"结束"的，而进度条停在 19%。

**改了两处**：
① 某个后端被摘掉时往 stderr 打一行，带上最后那条错误——少掉四分之一算力
   不该只表现为"变慢了"。
② 全部后端都被摘掉时抛 `RuntimeError`，明说「提前停止、剩下的任务从未尝试过」。

**对另一侧的影响**：没有。纯粹是失败可见性，不碰数据库也不改任何状态语义。
但如果你有脚本按「命令退出码 0」判断这一轮跑完了，**那个判断以前就是错的**，
现在会如实报错。

**验证**：244 passed / 25 skipped，ruff 干净。新增 2 个测试：全挂时抛错、
只挂一个时任务继续且 stderr 有告警。

**仍待查（不是这次改动能解决的）**：为什么会有这么多失败。第二轮
`fallback.by_reason` 里 `timeout` 占 89%（960/1075），而 `request_timeout`
已经是 120 秒——一次整段翻译不该要两分钟。下一步要先确认 vLLM 实例当下是否
健康（`bench_backend.py` 量单条延迟），而不是继续调参数。

---

## 2026-09-04 · 撤回并发 128：我把两个参数往相反方向调，造成 86% 超时

**动了**：`configs/backend_config.json`（并发 128 → 64，`request_timeout` 60 → 120）

**为什么**：上一轮我同时做了方向相反的两件事——并发 64 → 128（队列变深）、
`request_timeout` 300 → 60（耐心变短）。用户跑了 36 分钟的结果：

```
processed 8135, written 4558, failed 1016
fallback  total 7049, by_reason {"timeout": 6930, ...}
```

**86% 的样本整段翻译超时**，全部退化成逐句，其中 98% 的回退触发原因是 `timeout`。
这是我的误判，不是回退上限本身的问题。

**认识上的修正**：`--max-num-seqs 128` 是**上限**，不是服务端真能同时跑的条数——
真实并发由 KV cache 决定。offered 并发超过它之后，多出来的只在服务端排队：
**不增加吞吐（吞吐由 GPU 定），只拉长每条的墙上时间**，于是撞上超时。
正确做法是让 concurrency 对齐 vLLM 日志里的 `Running: N`，而不是配置上限。
这条写进 `backend_config.json` 的注释了。

`request_timeout` 也没有回到 300：回退现在有 `fallback_budget_seconds` 兜底，
不需要靠单次超时来防长尾，120 秒是个能吸收正常排队又不放任卡死的值。

**对另一侧的影响**：无代码改动，纯配置。但你那份按 `latency_ms` 的诊断，
**60 秒那一轮的数据要整段丢弃**——那不是稳态，是我调坏的窗口。

**待观察**：重启后 `fallback.jsonl` 里 `timeout` 的占比应该大幅下降。
如果降下来之后主因变成 `too_few_turns`，那才是真问题（`max_tokens` 不够、
长对话被截断），处方和 `not_json`（提示词太松）相反——这正是加分类计数的意义。

---

## 2026-09-04 · `request_timeout` / `fallback_budget_seconds` 两个字段名当作契约

**动了**：`src/finevision_to_sharegpt/config_loader.py`（只加了一段注释）

**为什么**：Claude 1 的后端吞吐诊断脚本按名字读 `BackendPoolConfig` 的
`request_timeout` 和 `fallback_budget_seconds`。他有 `getattr` 兜底，改名不会崩，
**但上限会被算少而没人察觉**——这种坏法比直接报错难查得多。

把这句写在字段旁边而不是只留在日志里：下一个想重命名的人是在 dataclass 上
动手的，翻不到协作日志。

**对另一侧的影响**：无功能改动。这两个名字我这边视为对外契约，
要改会先在日志里说。

---

## 2026-09-04 · 回退加两道上限，触发原因可计数，单次超时收到 60 秒

**动了**：`translator.py`、`config_loader.py`（**共有，按约定声明**）、
`cli.py`（**共有，按约定声明**）、`configs/backend_config.json`、`tests/test_translator.py`

**为什么**：你那份诊断查得很准，三条我都按你说的做了。

**① 回退加总预算（根因）。** 原来是每句吃满 `request_timeout`、句数不限，
一条 39 轮的对话就是 3.27 小时。现在两道上限：

- `fallback_max_turns`（默认 12）：**在花掉任何时间之前就判死**。
  39 轮的对话根本不进逐句路径——它值不了那么多吞吐。
- `fallback_budget_seconds`（默认 300）：整条样本在回退上总共能花多久。
  每次调用只拿**剩余**预算（`min(timeout, remaining)`），所以最后一句也不会超支。

超了就判失败进 `failed.jsonl`，之后可以单独起一轮重试——一条难样本不值得
拿几千条的吞吐去换。

**② 触发原因现在有分类计数。** 新增 `ParseFailure`，带一个短 `code`：
`not_json` / `not_object` / `no_conversations` / `too_few_turns` /
`turn_not_object` / `role_order` / `empty_value`，客户端侧的归 `timeout` 或
`other:<异常名>`。`translate_sample` 多了 `on_fallback(code, turns)` 回调，
CLI 把它计数并**逐条写 `fallback.jsonl`**（和 `rejected`/`failed` 放一起），
最终统计里也多一段 `"fallback": {"total": …, "by_reason": {…}}`。

十几天的任务，中途 grep 得到才有意义：

```bash
python -c "
import json,collections
c=collections.Counter(json.loads(l)['reason'] for l in open('<输出目录>/fallback.jsonl'))
print(c.most_common())"
```

`too_few_turns` 占多数 → 是 `max_tokens` 不够、长对话被截断；
`not_json` 占多数 → 模型在输出散文，提示词要收紧。**这两个的处方相反，
所以之前没有计数是查不下去的。**

**③ `request_timeout` 300 → 60。** 翻一句不该要五分钟。副作用是最重的样本
可能超时进 `failed.jsonl`，这是有意的取舍：宁可漏掉少数难样本，也不要让它们
占住线程。

**对另一侧的影响**：

1. **`failed.jsonl` 的 `error` 字段格式变了**，现在是
   `"<触发原因> -> fallback: <回退失败原因>"`（例如
   `"not_json -> fallback: 39 turns exceeds the fallback cap of 12"`）。
   你要是有按 error 前缀分类的查询或脚本，得跟着改。
2. `latency_ms` 的含义没变（仍是整个 `translate_sample`），但**分布会完全不同**
   ——最慢那条从 1.18e7 ms 应该降到 3e5 ms 量级。你之前那份按 `latency_ms`
   排序的诊断，改动前后的数不可直接比较。
3. 数据库侧没有任何改动。

**验证**：242 passed / 25 skipped，`ruff --select F,E9` 干净。新增 7 个测试：
超句数的对话一次回退都不发（`client.calls == 1`）、预算耗尽会中断且调用数
远少于句数、每次调用拿到的超时递减且不超预算、短对话仍能正常走完回退、
两种触发原因能被分开计数。用假时钟推进，测试不真等。

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
